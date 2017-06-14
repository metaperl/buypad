# core
import ConfigParser
from datetime import datetime
import logging
import os
import pprint
import sys
import time
import traceback

# 3rd party
from argh import dispatch_command, arg
from tabulate import tabulate

# local
import exchange as _exchange
import exception
from mynumbers import F, CF


# os.chdir("/home/schemelab/prg/adsactly-gridtrader/src")

# If any grid position's limit order has this much or less remaining,
# consider it totally filled
epsilon = 1e-8

def display_balances(e):
    balances = get_balances(e)

    markets = balances.keys()
    pairs = ""
    for m in sorted(markets):
        if m == 'BTC':
            continue
        pairs += "BTC-" + m + " "
    pairs = "pairs: {}\n".format(pairs)

    coinstr = ""
    for coin in sorted(balances.keys()):
        amounts = int(balances[coin]['TOTAL'])
        coinstr +=  "{}: {}\n".format(coin, amounts)

    logging.debug("""
[pairs]
{}

[initialcorepositions]
{}
""".format(pairs, coinstr))


def _set_balances(exchange, config_filename, config):
    section = 'initialcorepositions'
    config.remove_section(section)
    config.add_section(section)

    balances = get_balances(exchange)
    for coin in sorted(balances.keys()):
        logging.debug("COIN %s", coin)
        config.set(section, coin, balances[coin]['TOTAL'])
    logging.debug("Writing data to %s:", config_filename)
    with open(config_filename, 'w') as configfile:
        config.write(configfile)

def display_session_info(session_args, e, end=False):
    session_date = datetime.now().strftime('%a, %d %b %Y %H:%M:%S +0000')
    forward_slash = "/" if end else ""

    balances = get_balances(e)
    balstr = ""
    for coin in sorted(balances.keys()):
        amounts = balances[coin]
        balstr += "{}={},".format(coin, amounts['TOTAL'])

    logging.debug("<{}session args={} balances={} date={} >".format(
        forward_slash, session_args, balstr, session_date)
    )


def config_file_name(account):
    return "config/{}.ini".format(account)


def persistence_file_name(exch):
    return "persistence/{0}.storage".format(exch)

def pair2currency(pair):
    btc, currency = pair.split('-')
    return currency


def percent2ratio(i):
    return i / 100.0


def delta_by_ratio(v, r):
    return v + v * r


def delta_by_percent(v, p):
    r = percent2ratio(p)
    return delta_by_ratio(v, r)


def i_range(a):
    l = len(a)
    if not l:
        return "zero-element list"
    else:
        return "from {0} to {1}".format(0, len(a)-1)


class TradePad(object):

    def __init__(self, config):
        self.config = config

    def __str__(self):
        s = str()
        for market in self.grids:
            s += '<{0}>\n'.format(market)
            for buysell in self.grids[market]:
                s += str(self.grids[market][buysell])
            s += '</{0}>\n'.format(market)

        return "{0}\n{1}".format(type(self).__name__, s)

    def rate_for(self, exchange, mkt, btc):
        "Return the rate that works for a particular amount of BTC."

        coin_amount = 0
        btc_spent = 0
        logging.debug("Getting sell order book for %s", mkt)
        orders = exchange.returnSellOrderBook(mkt)
        for order in orders:
            btc_spent += order['Rate'] * order['Quantity']
            if btc_spent > btc:
                break

        coin_amount = btc / order['Rate']
        return order['Rate'], coin_amount

    def btc(self, exchange):
        b = exchange.returnBalance('BTC')
        b = b['Available']
        logging.debug("BAL: %s", b)
        return b

    def execute(self):
        exchange_name = 'bittrex'
        exchange = _exchange.exchangeFactory(exchange_name, self.config)
        for market, btc_to_spend in self.config.items(exchange_name):
            rate, amount = self.rate_for(exchange, market, btc_to_spend)
            exchange.buy(market, rate, amount)

    @property
    def pairs(self):

        pairs = dict()

        for pair in self.config.get('pairs', 'pairs').split():
            logging.debug("pair: {}".format(pair))
            pairs[pair] = self.exchange.tickerFor(pair)

        return pairs

    @property
    def balances(self):
        return self.exchange.returnBalances()

    def midpoint(self, pair):
        pair_info = self.exchange.tickerFor(pair)
        logging.debug("Pair info in Midpoint = {}".format(pair_info))
        return (F(pair_info.lowestAsk) + F(pair_info.highestBid))/ 2.0

    def config_core(self):
        pass

    def build_new_grids(self):

        grid = dict()
        logging.debug("Creating buy and sell grids")
        for pair in self.pairs:
            grid[pair] = dict()
            grid[pair]['sell'] = SellGrid(
                pair=pair,
                current_market_price=self.midpoint(pair),
                gridtrader=self
            )
            grid[pair]['buy'] = BuyGrid(
                pair=pair,
                current_market_price=self.midpoint(pair),
                gridtrader=self
            )
            for direction in 'sell buy'.split():
                logging.debug(
                   "{0} grid = {1}".format(direction, grid[pair][direction]))


        self.grids = grid

    def issue_trades(self):
        for market in self.grids:
            self.market[market] = {
                'lowestAsk'  : F(self.exchange.tickerFor(market).lowestAsk),
                'highestBid' : F(self.exchange.tickerFor(market).highestBid),
            }
            for buysell in self.grids[market]:
                g = self.grids[market][buysell]

                if buysell in 'buy sell':
                    try:
                        g.place_orders(self.exchange)
                    except (exception.NotEnoughCoin, exception.DustTrade):
                        logging.debug("%s grid not fully created because there was not enough coin", buysell)
                else:
                    raise exception.InvalidDictionaryKey("Key other than buy or sell: %s", buysell)


    def poll(self):

        for market in self.grids:
            logging.debug("Analyzing %s. Our current holdings = %s", market, self.exchange.returnBalanceFromMarket(market))

            g = self.grids[market]

            logging.debug("Checking %s buy activity", market)
            deepest_i = g['buy'].trade_activity(self.exchange)
            if deepest_i is None:
                logging.debug(
                    "No %s buy trade activity detected %s",
                    market, i_range(g['buy'].trade_ids)
                    )
            else:
                gb = g['buy']
                logging.debug(
                    "%s Buy trade activity detected at index %d of %d",
                       market, deepest_i, len(gb.trade_ids)-1)
                for i in xrange(deepest_i, -1, -1):
                    fill_rate = gb.grid[i]
                    logging.debug("Buy rate @i={0} == {1}".format(i, fill_rate))
                    logging.debug("Let's see our holdings %s", self.exchange.returnBalanceFromMarket(market))


                    profit_target = gb.profitTarget
                    if profit_target <= 0:
                        logging.debug("Accumulating purchase instead of selling for profit")
                    else:
                        sell_rate = delta_by_percent(fill_rate, profit_target)
                        logging.debug(
                            "Creating sell trade size={0} rate={1}".format(
                                gb.size, sell_rate))
                        self.exchange.sell(market, amount=gb.size, rate=sell_rate)

                gb.purge_closed_trades(deepest_i)

                if not gb.trade_ids:
                    logging.debug(
                        """
%s Buy grid exhausted. Creating new buy grid.
Current market conditions: highestBid = %f, lowestAsk = %f
                        """,
                        market,
                        self.exchange.tickerFor(market).highestBid,
                        self.exchange.tickerFor(market).lowestAsk
                    )
                    deepest_filled_rate = self.exchange.tickerFor(market).highestBid
                    self.grids[market]['buy'] = BuyGrid(
                        pair=market,
                        current_market_price=deepest_filled_rate,
                        gridtrader=self
                    ).place_orders(self.exchange)

            logging.debug("Checking %s sell activity", market)
            deepest_i = g['sell'].trade_activity(self.exchange)
            if deepest_i is None:
                logging.debug(
                    "No %s sell trade activity detected %s",
                    market, i_range(g['sell'].trade_ids)
                )
            else:
                logging.debug(
                    "%s Sell trade activity detected at index %d of %d",
                    market, deepest_i, len(g['sell'].trade_ids)-1)

                deepest_filled_rate = g['sell'].grid[deepest_i]
                logging.debug("Deepest filled rate = %f", deepest_filled_rate)

                g['sell'].purge_closed_trades(deepest_i)

                logging.debug(
                    "Cancelling and elevating the %s buy grid", market)
                self.exchange.cancelOrders(
                    self.grids[market]['buy'].trade_ids)
                self.grids[market]['buy'] = BuyGrid(
                    pair=market,
                    current_market_price=deepest_filled_rate,
                    gridtrader=self
                ).place_orders(self.exchange)

            if not g['sell'].trade_ids:
                logging.debug(
                    "%s Sell grid exhausted. Creating new sell grid",
                    market)
                deepest_filled_rate = self.exchange.tickerFor(market).lowestAsk
                g['sell'] = SellGrid(
                    pair=market,
                    current_market_price=deepest_filled_rate,
                    gridtrader=self
                ).place_orders(self.exchange)

    def notify_admin(self, error_msg):

        import mymailer
        mymailer.send_email(self.account, error_msg)


def delta(percent, v):
    return v + percent2ratio(percent) * v


def pdict(d, skip_false=True):
    parms = list()
    for k in sorted(d.keys()):
        if not d[k] and skip_false:
            continue
        parms.append("{0}={1}".format(k, d[k]))

    return ",".join(parms)

# http://stackoverflow.com/questions/5595425/what-is-the-best-way-to-compare-floats-for-almost-equality-in-python
def isclose(a, b, rel_tol=epsilon, abs_tol=0.0):
    return abs(a-b) <= max(rel_tol * max(abs(a), abs(b)), abs_tol)

def iszero(v):
    # logging.debug("isclose(0, %f)", v)
    # return isclose(0, v)
    return v < epsilon

def get_balances(e):

    b = e.returnPositiveBalances()

    return b

def print_balances(e):
    b = get_balances(e)
    logging.debug(b.pformat())


def initialize_logging(account_name, args):

    args = pdict(args)

    rootLogger = logging.getLogger()

    logPath = 'log/{}'.format(account_name)
    fileName = "{0}--{1}".format(
        time.strftime("%Y%m%d-%H %M %S"),
        args
        )

    fileHandler = logging.FileHandler(
        "{0}/{1}.log".format(logPath, fileName))
    #fileHandler.setFormatter(logFormatter)
    rootLogger.addHandler(fileHandler)

    consoleHandler = logging.StreamHandler(stream=sys.stdout)
    #consoleHandler.setFormatter(logFormatter)
    rootLogger.addHandler(consoleHandler)

    return args, fileName

def main_init(exchange, gt, persistence_file):
    exchange.cancelAllOpen()

    logging.debug("Building trade grids")
    gt.build_new_grids()

    logging.debug("Issuing trades on created grids", 1)
    gt.issue_trades()

    logging.debug("Storing GridTrader to disk.")
    Persist(persistence_file).store(gt)

@arg('account', help="The account whose API keys we are using (e.g. terrence, joseph, peter, etc.")
def main(
        account,
):

    command_line_args = locals()

    args, fileName = initialize_logging(account, command_line_args)

    config_file = config_file_name(account)
    config = ConfigParser.RawConfigParser()
    config.read(config_file)

    tp = TradePad(config)
    tp.execute()


if __name__ == '__main__':
    dispatch_command(main)
