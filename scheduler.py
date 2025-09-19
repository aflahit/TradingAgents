from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.default_config import DEFAULT_CONFIG
import pandas_market_calendars as mcal
from datetime import datetime

import schedule
import time
import signal

ta = TradingAgentsGraph(debug=True, config=DEFAULT_CONFIG.copy())

ticker = 'GOOG'

def market_is_open(date):
    result = mcal.get_calendar("NASDAQ").schedule(start_date=date, end_date=date)
    return result.empty == False

run = True

def handler_stop_signals(signum, frame):
    global run
    run = False

signal.signal(signal.SIGINT, handler_stop_signals)
signal.signal(signal.SIGTERM, handler_stop_signals)

def job():
    print("Starting job...")
    today = datetime.now().strftime("%Y-%m-%d")
    is_market_open = market_is_open(today)
    print(is_market_open)
    # get insights
    if is_market_open:
        _, decision = ta.propagate("GOOG", today)
        with open('output.txt', 'a') as f:
            f.write(f"""{today}, {decision}\n""")
    else:
        with open('output.txt', 'a') as f:
            f.write(f"""{today}, {'Holiday'}\n""")
    


# schedule.every(10).seconds.do(job)
# schedule.every().hour.do(job)
schedule.every().day.at("20:30").do(job)

while run:
    schedule.run_pending()
    time.sleep(1)