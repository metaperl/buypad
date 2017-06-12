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
from persist import Persist

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


def config_file_name(exchange, account):
    return "config/{}/{}.ini".format(exchange, account)


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


class Grid(object):
    def __init__(
            self, pair, current_market_price, gridtrader):

        logging.debug("Initializing %s grid with current market price = %.8f",
                      pair, current_market_price)

        self.trade_ids = list()

        self.pair = pair
        self.current_market_price = current_market_price
        self.gridtrader = gridtrader
        self.make_grid()

    @property
    def initial_core_position(self):
        return self.config.getfloat(
            'initialcorepositions', self.exchange.baseOf(self.pair))

    @property
    def exchange(self):
        return self.gridtrader.exchange

    @property
    def config(self):
        return self.gridtrader.config

    @property
    def majorLevel(self):
        return percent2ratio(CF(self.config, self.config_section, 'majorLevel'))

    @property
    def numberOfOrders(self):
        return self.config.getint(self.config_section, 'numberOfOrders')

    @property
    def increments(self):
        return percent2ratio(
            self.config.getfloat(self.config_section, 'increments'))

    @property
    def config_section(self):
        return self.__class__.__name__.lower()

    @property
    def size(self):

        return (
            percent2ratio(self.config.getfloat(self.config_section, 'size'))
            * self.initial_core_position
            / self.numberOfOrders
        )


    def trade_activity(self, exchange):
        for i in xrange(len(self.trade_ids)-1, -1, -1):
            uuid = self.trade_ids[i]
            if not self.exchange.isOpen(uuid):
                return i

        return None

    def purge_closed_trades(self, deepest_i):
        new_grid = list()
        new_trade_ids = list()
        for i in xrange(0, len(self.trade_ids)):
            if i > deepest_i:
                new_grid.append(self.grid[i])
                new_trade_ids.append(self.trade_ids[i])

        self.grid = new_grid
        self.trade_ids = new_trade_ids

    def __str__(self):

        config_s = str()
        for grid_section in 'sellgrid buygrid'.split():
            config_s += "<{0}>".format(grid_section)
            for option in self.config.options(grid_section):
                config_s += "{0}={1}".format(
                    option, self.config.get(grid_section, option))
            config_s += "</{0}>".format(grid_section)


        table = [
            ["Core Position", self.initial_core_position],
            ["Pair", self.pair],
            ["Current Market Price", self.current_market_price],
            ["Grid Config", config_s],
            ["Size", self.size],
            ["Starting Price", self.starting_price],
            ["Grid", self.grid],
            ["Grid Trade Ids", self.trade_ids],
        ]

        myname = type(self).__name__
        return "  <{}>\n{}\n  </{}>\n".format(myname, tabulate(table, tablefmt="plain"), myname)

class SellGrid(Grid):

    def __init__(self, pair, current_market_price, gridtrader):
        super(type(self), self).__init__(
            pair, current_market_price, gridtrader)

    @property
    def starting_price(self):

        return (
            self.current_market_price +
            self.current_market_price * self.majorLevel
        )

    def make_grid(self):
        retval = list()
        last_price = self.starting_price
        for i in range(0, self.numberOfOrders):
            retval.append(last_price)
            next_price = last_price + last_price * self.increments
            # print next_price
            last_price = next_price

        self.grid = retval

    def place_orders(self, exchange):
        logging.debug("<PLACE_ORDERS>")

        for rate in self.grid:
            r = exchange.sell(self.pair, rate, self.size)
            self.trade_ids.append(r.orderNumber)

        logging.debug("</PLACE_ORDERS>")

        return self

class BuyGrid(Grid):

    def __init__(self, pair, current_market_price, gridtrader):
        super(type(self), self).__init__(
            pair, current_market_price, gridtrader)

    @property
    def starting_price(self):
        return (
            self.current_market_price -
            self.current_market_price * self.majorLevel
        )

    @property
    def profitTarget(self):
        return self.config.getfloat(self.config_section, 'profitTarget')

    def make_grid(self):
        retval = list()
        last_price = self.starting_price
        for i in range(0, self.numberOfOrders):
            retval.append(last_price)
            next_price = last_price - last_price * self.increments
            # print next_price
            last_price = next_price

        self.grid = retval

    def place_orders(self, exchange):
        print "<PLACE_ORDERS>"

        self.remaining = dict()

        for rate in self.grid:
            r = exchange.buy(self.pair, rate, self.size)
            self.trade_ids.append(r.orderNumber)

        print "</PLACE_ORDERS>"

        return self


class GridTrader(object):

    def __init__(self, exchange, config, account):
        self.exchange, self.config = exchange, config
        self.account = account
        self.market = dict()

    def __str__(self):
        s = str()
        for market in self.grids:
            s += '<{0}>\n'.format(market)
            for buysell in self.grids[market]:
                s += str(self.grids[market][buysell])
            s += '</{0}>\n'.format(market)

        return "{0}\n{1}".format(type(self).__name__, s)


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


def initialize_logging(exchange_name, account_name, args):

    args = pdict(args)

    rootLogger = logging.getLogger()

    logPath = 'log/{}/{}'.format(exchange_name, account_name)
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
            
@arg('--cancel-all', help="Cancel all open orders, even if this program did not open them")
@arg('--init', help="Create new trade grids, issue trades and persist grids.")
@arg('--monitor', help="See if any trades in grid have closed and adjust accordingly")
@arg('--status-of', help="(Developer use only) Get the status of a trade by trade id")
@arg('account', help="The account whose API keys we are using (e.g. terrence, joseph, peter, etc.")
@arg('exchange-name', help="on which exchange (polo, trex, gdax)")
@arg('--balances', help="list coin holdings")
@arg('--set-balances', help="Alter [initialcorepositions] section of config file based on exchange holdings.")
def main(
        exchange_name,
        account,
        cancel_all=False,
        init=False,
        monitor=False,
        balances=False,
        set_balances=False,
        status_of='',
):

    command_line_args = locals()

    args, fileName = initialize_logging(exchange_name, account, command_line_args)

    config_file = config_file_name(exchange_name, account)
    config = ConfigParser.RawConfigParser()
    config.read(config_file)

    persistence_file = persistence_file_name(account)

    exchange = _exchange.exchangeFactory(exchange_name, config)

    display_session_info(args, exchange)

    gt = GridTrader(exchange, config, account)

    try:

        if cancel_all:
            exchange.cancelAllOpen()

        if init:
            main_init(exchange, gt, persistence_file)

        if monitor:
            logging.debug("Evaluating trade activity since last invocation")
            persistence = Persist(persistence_file)
            gt = persistence.retrieve()
            gt.poll()
            persistence.store(gt)

        if balances:
            logging.debug("Getting balances")
            display_balances(exchange)

        if set_balances:
            logging.debug("Setting balances")
            _set_balances(exchange, config_file, config)
            main_init(exchange, gt, persistence_file)
        
        if status_of:
            logging.debug("Getting status of order")
            get_status_of(status_of)

    except Exception as e:
        error_msg = traceback.format_exc()
        logging.debug('Aborting: %s', error_msg)
        gt.notify_admin(error_msg)


    display_session_info(args, exchange, end=True)

if __name__ == '__main__':
    dispatch_command(main)
