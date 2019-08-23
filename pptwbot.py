import re
import time
import discord
import asyncio
import calendar
import logging
import logging.config
from os import environ
from datetime import date, timedelta, datetime
from decimal import Decimal
from tastyworks.models import option_chain, underlying
from tastyworks.models.option import Option, OptionType
from tastyworks.models.order import Order, OrderDetails, OrderPriceEffect, OrderType, OrderStatus
from tastyworks.models.session import TastyAPISession
from tastyworks.models.trading_account import TradingAccount
from tastyworks.models.alert import Alert, AlertField, Operator
from tastyworks.models.position import Position
from tastyworks.models.underlying import UnderlyingType
from tastyworks.streamer import DataStreamer
from tastyworks.tastyworks_api import tasty_session
from settings import Settings

# Create logger.
LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(logging.DEBUG)
# Create filehandler with desired filename.
fh = logging.FileHandler('PPTWBot_{}.log'.format(datetime.now().strftime('%Y_%m_%d')))
fh.setLevel(logging.DEBUG)
log_formatter = logging.Formatter('%(asctime)s | %(levelname)-8s | %(lineno)04d | %(message)s')
fh.setFormatter(log_formatter)
# Add filehandler to logger.
LOGGER.addHandler(fh)
# LOGGER.addHandler(logging.StreamHandler())

client = discord.Client()

@client.event
async def on_ready():
    LOGGER.info('Logged in as')
    LOGGER.info(client.user.name)
    LOGGER.info(client.user.id)
    LOGGER.info('------')
    for server in client.servers:
        if server.name == 'Profit Planet Pro':
            LOGGER.info ('{0} Server ID: {1}'.format(server.name, server.id))
            for channel in server.channels:
                if channel.name == Settings.alert_channel:
                    LOGGER.info ('Signals Channel Name: {0} ID: {1}'.format(channel.name, channel.id))
                if channel.name == Settings.test_channel:
                    LOGGER.info ('Test Channel Name: {0} ID: {1}'.format(channel.name, channel.id))
                if channel.name == Settings.chat_channel:
                    LOGGER.info ('Trading Floor Channel Name: {0} ID: {1}'.format(channel.name, channel.id))

@client.event
async def on_message(message):
    if message.channel.name == Settings.alert_channel:
        LOGGER.info ('Options signal received: {}'.format(message.content))
        message_ucase = message.content.upper()
        Entry = re.search(Settings.EntryRegex, message_ucase)
        Update = re.search(Settings.UpdateRegex, message_ucase)
        Deactivate = re.search(Settings.DeactivateRegex, message_ucase)
        if Entry:
            await ProcessEntrySignal(Entry)
        elif Update:
            await ProcessUpdateSignal(Update)
        elif Deactivate:
            await ProcessDeactivationSignal(Deactivate)
    elif message.channel.name == Settings.test_channel:
        message_ucase = message.content.upper()
        Entry = re.search(Settings.EntryRegex, message_ucase)
        Update = re.search(Settings.UpdateRegex, message_ucase)
        Deactivate = re.search(Settings.DeactivateRegex, message_ucase)
        if Entry:
            if Settings.execute_from_test_channel and message.author.name == client.user.name:
                await ProcessEntrySignal(Entry)
            await client.add_reaction(message, '\N{THUMBS UP SIGN}')
        elif Update:
            if Settings.execute_from_test_channel and message.author.name == client.user.name:
                await ProcessUpdateSignal(Update)
            await client.add_reaction(message, '\N{THUMBS UP SIGN}')
        elif Deactivate:
            if Settings.execute_from_test_channel and message.author.name == client.user.name:
                await ProcessDeactivationSignal(Deactivate)
            await client.add_reaction(message, '\N{THUMBS UP SIGN}')
    elif message.channel.name == Settings.chat_channel:
        print ('{0}:@{1} - {2}'.format(message.channel.name, message.author.name, message.content))

# Entry Processing

async def ProcessEntrySignal(Entry):
    Ticker = Entry.group('Ticker').upper()
    ExpDate = get_expire_date_from_string(Entry.group('Exp'))
    Strike = Decimal(Entry.group('Strike'))
    Mark = Decimal(Entry.group('Mark'))
    Price = Decimal(Entry.group('Entry')) + Settings.IncreaseEntryLimitOrderBy
    Quantity = int(Settings.MaxBet/(Price * 100))
    if Quantity > Settings.MaxContracts:
        Quantity = Settings.MaxContracts
    if Entry.group('Type').upper() == 'CALL':
        opt_type = OptionType.CALL
    elif Entry.group('Type').upper() == 'PUT':
        opt_type = OptionType.PUT
    LOGGER.info ('{} Entry signal received. Ticker: {} Strike: {} Exp: {} Entry: {} Mark: {}'.format(opt_type, Ticker, Strike, ExpDate, Price, Mark))
    if Quantity > 0:
        if not Ticker in Settings.AvoidStocks:
            LOGGER.info ('Canceling any existing orders for the ticker')
            await CancelOrderByTicker(Ticker)
            LOGGER.info ('Removing any alerts previously set for the ticker.')
            await DeleteAlertByTicker(Ticker)
            LOGGER.info ('Executing order with qty of {}'.format(Quantity))
            ret = await EnterTrade(Ticker, Price, ExpDate, Strike, opt_type, Quantity)
            LOGGER.info('Returned Data: {}'.format(ret))
            # Sleep for 5 seconds to allow time to get a fill.
            await asyncio.sleep(5)
            LOGGER.info('Calling ProcessUpdateSignal to place initial stop alerts...')
            await ProcessUpdateSignal(Entry)
        else:
            LOGGER.info ('Ticker is in the Avoid list.  Skipping.')
    else:
        LOGGER.info ('Quantity is 0 due to max bet.  No action taken.')
# Update Processing

async def ProcessUpdateSignal(Update):
    Ticker = Update.group('Ticker').upper()
    ExpDate = get_expire_date_from_string(Update.group('Exp'))
    Strike = Decimal(Update.group('Strike'))
    StockStop = Decimal(Update.group('StockStop'))
    OptionStop = Decimal(Update.group('OptionStop'))
    CurrentPrice = Decimal(Update.group('Mark'))
    LOGGER.info ('Update received for {0} Mark: {1} StockStop: {2} OptionStop: {3}'.format(Ticker, CurrentPrice, StockStop, OptionStop))
    LOGGER.info ('Getting Positions for {}...'.format(Ticker))
    positions = await GetPositions(Ticker)
    LOGGER.info ('Getting Orders for {}...'.format(Ticker))
    orders = await GetActiveOrders(Ticker)
    if positions:
        LOGGER.info ('Found {} open position(s) for {}...'.format(len(positions), Ticker))
        for position in positions:
            option_obj = position.get_option_obj()
            if option_obj.option_type == OptionType.CALL:
                stockstop_adjusted = StockStop - Settings.AdjustStockPriceAlertBy
            elif option_obj.option_type == OptionType.PUT:
                stockstop_adjusted = StockStop + Settings.AdjustStockPriceAlertBy
            LOGGER.info('Setting an alert for {} at adjusted stock price of ${}'.format(Ticker, stockstop_adjusted))
            ret = await SetAlertForPosition(position, stockstop_adjusted)
            LOGGER.info('Returned Data: {}'.format(ret))
    elif orders:
        LOGGER.info ('Found {} open order(s) for {}...'.format(len(orders), Ticker))
        for order in orders:
            if (CurrentPrice - order.details.price) > Settings.EntryPriceDriftLimit:
                LOGGER.info ('The price has drifted more than ${} without taking a position.  Cancelling order {}.'.format(Settings.EntryPriceDriftLimit, order.details.order_id))
                result = await CancelOrderByID(order.details.order_id)
                LOGGER.info('Final Result: {}'.format(result))
    else:
        LOGGER.info('No orders or positions found.  No action taken.')

# Exit Strategies

async def ProcessDeactivationSignal(Deactivate):
    Ticker = Deactivate.group('Ticker')
    ExpDate = get_expire_date_from_string(Deactivate.group('Exp'))
    Strike = Decimal(Deactivate.group('Strike'))
    LOGGER.info('Received deactivate message for {}'.format(Ticker))
    positions = await GetPositions(Ticker)
    if positions:
        for position in positions:
            LOGGER.info('Position found. Canceling all buy orders for {}'.format(Ticker))
            await CancelBuyOrdersByTicker(Ticker)
            if Settings.MarketSellOnDeactivate:
                LOGGER.info('Closing {} contract(s) for {} at market price'.format(position.quantity, Ticker))
                result = await ExitTradeWithMarketOrder(position)
                LOGGER.info('Returned Data: {}'.format(result))
                if result:
                    LOGGER.info('Removing any alerts that were set.')
                    await DeleteAlertByTicker(Ticker)
                else:
                    LOGGER.info('Error creating a market sell order.')
    else:
        LOGGER.info('No open positions for {}. Cancelling any orders.'.format(Ticker))
        await CancelOrderByTicker(Ticker)
        LOGGER.info('Removing any alerts....')
        await DeleteAlertByTicker(Ticker)

async def WatchAlertsAndExitIfTriggered():
    await asyncio.sleep(5)
    LOGGER.info('Starting Alerts Monitor')
    while Settings.MarketSellOnAlert:
        try:
            alerts = await GetTriggeredAlerts()
            for alert in alerts:
                LOGGER.info('The alert for {} at ${} has triggered.  Checking for and closing any opened positions.'.format(alert.symbol, alert.threshold))
                positions = await GetPositions(alert.symbol)
                if positions:
                    for position in positions:
                        LOGGER.info('Closing {} contract(s) for {} at market price'.format(position.quantity, alert.symbol))
                        await CancelOrderByTicker(alert.symbol)
                        result = await ExitTradeWithMarketOrder(position)
                        if result:
                            LOGGER.info('Removing the alert.')
                            await DeleteAlertByTicker(alert.symbol)
                else:
                    LOGGER.info('No open positions for {}.  Removing the alert.'.format(alert.symbol))
                    await DeleteAlertByTicker(alert.symbol)
        except Exception as ex:
            LOGGER.info('Unhandled exception in WatchAlertsAndExitIfTriggered()')
            LOGGER.fatal(ex, exc_info=True)
        await asyncio.sleep(2)

async def WatchPositionsAndExitAtPercentage():
    await asyncio.sleep(10)
    LOGGER.info('Starting Position Profit/Loss Monitor')
    while Settings.AutoCloseAtProfitPercent > 0 or Settings.AutoCloseAtLossPercent > 0:
        try:
            positions = await TradingAccount.get_positions(tasty_client, tasty_acct)
            for position in positions:
                profit_percent = get_profit_percent(position)
                LOGGER.info('Current profit for {} is {:.3f}%. Mark: ${:.3f} Entry: {} @ ${:.3f}'.format(position.underlying_symbol, profit_percent, position.mark_price, position.quantity, position.average_open_price))
                if Settings.AutoCloseAtProfitPercent > 0 and profit_percent >= Settings.AutoCloseAtProfitPercent:
                    if Settings.UseStopMarketOrderForProfitPercentExit:
                        existing_stop_order = False
                        replace_order = False
                        orders = await GetSellOrdersByTicker(position.underlying_symbol)
                        stop_trigger = position.mark_price - Settings.ProfitPercentExitTriggerPriceDelta
                        if stop_trigger <= position.average_open_price:
                            stop_trigger = position.average_open_price + Decimal('.01')
                        if orders:
                            for order in orders:
                                if order.details.type == OrderType.STOP:
                                    existing_stop_order = True
                                    if order.details.stop_trigger < stop_trigger:
                                        LOGGER.info('Increasing the stop trigger for {} from ${:.3f} to ${:.3f}'.format(position.underlying_symbol, order.details.stop_trigger, stop_trigger))
                                        await CancelOrderByID(order.details.order_id)
                                        replace_order = True
                            if replace_order:
                                await ExitTradeWithStopMarketOrder(position = position, stop_trigger = stop_trigger)
                        if existing_stop_order == False and replace_order == False:
                            LOGGER.info('Creating initial stop order for {} with a stop trigger of ${:.3f}'.format(position.underlying_symbol, stop_trigger))
                            await ExitTradeWithStopMarketOrder(position = position, stop_trigger = stop_trigger)
                            # await ExitTradeWithStopLimitOrder(position = position, price = limit_price, stop_trigger = stop_trigger)
                    else:
                        LOGGER.info('Canceling all opened orders for {}...'.format(position.underlying_symbol))
                        await CancelOrderByTicker(position.underlying_symbol)
                        LOGGER.info('Creating exit limit order for {}...'.format(position.underlying_symbol))
                        await ExitTradeWithLimitOrder(position = position, price = position.mark_price)
                elif Settings.AutoCloseAtLossPercent > 0 and (profit_percent * -1) >= Settings.AutoCloseAtLossPercent:
                    LOGGER.info('A loss of {:.3f}% or greater has been detected for {} at market price of ${:.3f}.  Closing position with market sell order.'.format(Settings.AutoCloseAtLossPercent, position.underlying_symbol, position.mark_price))
                    await CancelSellOrdersByTicker(position.underlying_symbol)
                    await ExitTradeWithMarketOrder(position = position)
        except Exception as ex:
            LOGGER.info('Unhandled exception in WatchPositionsAndExitAtPercentage()')
            LOGGER.fatal(ex, exc_info=True)
        await asyncio.sleep(2)

# End Exits

async def EnterTrade(ticker, price: Decimal, expiry, strike: Decimal, opt_type: OptionType, quantity = 1):
    sub_values = {"Quote": ["/ES"]}
    if Settings.EnterWithMarketOrder:
        details = OrderDetails(type=OrderType.MARKET, price=None, price_effect=OrderPriceEffect.DEBIT)
    else:
        details = OrderDetails(type=OrderType.LIMIT, price=price, price_effect=OrderPriceEffect.DEBIT)
    new_order = Order(details)
    opt = Option(ticker=ticker, quantity=quantity, expiry=expiry, strike=strike, option_type=opt_type, underlying_type=UnderlyingType.EQUITY)
    new_order.add_leg(opt)
    return await tasty_acct.execute_order(new_order, tasty_client, dry_run=False)

async def ExitTradeWithLimitOrder(position: Position, price: Decimal):
    new_order = position.get_closing_order_object(price)
    return await tasty_acct.execute_order(new_order, tasty_client, dry_run=False)

async def ExitTradeWithStopLimitOrder(position: Position, price: Decimal, stop_trigger: Decimal):
    new_order = position.get_closing_order_object(price, stop_trigger, OrderType.STOP_LIMIT)
    return await tasty_acct.execute_order(new_order, tasty_client, dry_run=False)

async def ExitTradeWithStopMarketOrder(position: Position, stop_trigger: Decimal):
    new_order = position.get_closing_order_object(price=None, stop_trigger=stop_trigger, order_type=OrderType.STOP)
    return await tasty_acct.execute_order(new_order, tasty_client, dry_run=False)

async def ExitTradeWithMarketOrder(position: Position):
    new_order = position.get_closing_order_object(price=None, order_type=OrderType.MARKET)
    return await tasty_acct.execute_order(new_order, tasty_client, dry_run=False)

async def GetPositions(ticker: str) -> list:
    sub_values = {"Quote": ["/ES"]}
    positions = await TradingAccount.get_positions(tasty_client, tasty_acct)
    ret = []
    for position in positions:
        if position.underlying_symbol.upper() == ticker.upper():
            ret.append(position)
    return ret

async def GetActiveOrders(ticker: str) -> list:
    orders = await Order.get_live_orders(tasty_client, tasty_acct)
    ret = []
    for order in orders:
        if order.details.ticker.upper() == ticker.upper():
            ret.append(order)
    return ret

async def GetBuyOrdersByTicker(ticker: str) -> list:
    orders = await Order.get_live_orders(tasty_client, tasty_acct)
    ret = []
    for order in orders:
        if order.details.ticker.upper() == ticker.upper() and order.details.price_effect == OrderPriceEffect.DEBIT:
            ret.append(order)
    return ret

async def GetSellOrdersByTicker(ticker: str) -> list:
    orders = await Order.get_live_orders(tasty_client, tasty_acct)
    ret = []
    for order in orders:
        if order.details.ticker.upper() == ticker.upper() and order.details.price_effect == OrderPriceEffect.CREDIT:
            ret.append(order)
    return ret

async def CancelBuyOrdersByTicker(ticker: str, orders = None):
    if orders == None:
        orders = await GetActiveOrders(ticker)
    for order in orders:
        if order.details.ticker.upper() == ticker.upper() and order.details.price_effect == OrderPriceEffect.DEBIT:
            result = await CancelOrderByID(order.details.order_id)
            LOGGER.info ('Final Result: {}'.format(result))

async def CancelSellOrdersByTicker(ticker: str, orders = None):
    if orders == None:
        orders = await GetActiveOrders(ticker)
    for order in orders:
        if order.details.ticker.upper() == ticker.upper() and order.details.price_effect == OrderPriceEffect.CREDIT:
            result = await CancelOrderByID(order.details.order_id)
            LOGGER.info ('Final Result: {}'.format(result))

async def CancelOrderByTicker(ticker: str, orders = None):
    if orders == None:
        orders = await GetActiveOrders(ticker)
    for order in orders:
        result = await CancelOrderByID(order.details.order_id)
        LOGGER.info ('Final Result: {}'.format(result))

async def CancelOrderByID(order_id):
    result = await Order.cancel_order(tasty_client, tasty_acct, order_id)
    LOGGER.info ('Canceling order id {}.  Initial Result: {}'.format(order_id, result))
    while result == OrderStatus.CANCEL_REQUESTED:
        await asyncio.sleep(1)
        order = await Order.get_order(tasty_client, tasty_acct, order_id)
        result = order.details.status
    return result

async def DeleteAlertByTicker(ticker: str):
    alerts = await TradingAccount.get_quote_alert(tasty_client)
    for alert in alerts:
        if alert.symbol.upper() == ticker.upper():
            await TradingAccount.delete_quote_alert(tasty_client, alert)

async def SetAlertForPosition(position: Position, price: Decimal):
    await DeleteAlertByTicker(position.underlying_symbol)
    alert = position.get_last_stock_price_alert_oobject(price)
    return await TradingAccount.set_quote_alert(tasty_client, alert)

async def GetTriggeredAlerts() -> list:
    ret = []
    alerts = await TradingAccount.get_quote_alert(tasty_client)
    for alert in alerts:
        if alert.triggered == True:
            ret.append(alert)
    return ret

def get_expire_date_from_string(d):
    this_year = int(date.today().year)
    month = int(d.split('/')[0])
    day = int(d.split('/')[1])
    if date(this_year, month, day) >= date.today():
        return date(this_year, month, day)
    else:
        next_year = this_year + 1
        return date(next_year, month, day)

def get_profit_percent(position: Position):
    entry_fee = Decimal('1.00') * Decimal(position.quantity)
    entry = position.average_open_price * position.multiplier
    if entry_fee > 10:
        entry = entry + Decimal('10')
    else:
        entry = entry + entry_fee
    currentpl = (position.mark_price - position.average_open_price) * position.multiplier
    return Decimal((currentpl / entry) * 100)

tasty_client = tasty_session.create_new_session(Settings.tasty_user, Settings.tasty_password)
streamer = DataStreamer(tasty_client)
LOGGER.info('TW Streamer token: %s' % streamer.get_streamer_token())
tw_accounts = asyncio.new_event_loop().run_until_complete(TradingAccount.get_remote_accounts(tasty_client))
tasty_acct = None
for account in tw_accounts:
    if account.account_number == Settings.tasty_account_number and account.is_margin == False:
        tasty_acct = account
        LOGGER.info('TW Account found: %s', tasty_acct)
if not tasty_acct:
    raise Exception('Could not find a TastyWorks cash account with account number {} in the list of accounts: {}'.format(Settings.tasty_account_number, tw_accounts))

loop = asyncio.get_event_loop()

try:
    task1 = loop.create_task(client.start(Settings.Discord_Token, bot=False))
    task2 = loop.create_task(WatchAlertsAndExitIfTriggered())
    task3 = loop.create_task(WatchPositionsAndExitAtPercentage())
    loop.run_until_complete(asyncio.gather(task1, task2, task3))
except KeyboardInterrupt:
    pass
except Exception as ex:
    LOGGER.error('An unexpected error occurred in the main loop: {}'.format(ex.message))
    LOGGER.fatal(ex, exc_info=True)
finally:
    loop.run_until_complete(client.logout())
    loop.run_until_complete(client.close())
    time.sleep(3)
    loop.close()