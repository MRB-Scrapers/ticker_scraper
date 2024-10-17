import asyncio
import json
import os
import random
import re
import time
from datetime import datetime

import pytz
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from requestium import Keys, Session
from selenium.webdriver import ActionChains
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from seleniumrequests import Chrome
from utils.logger import log_message
from utils.telegram_sender import send_telegram_message
from utils.time_utils import get_next_market_times, sleep_until_market_open
from utils.websocket_sender import send_ws_message

load_dotenv()

# Constants
HEDGEYE_SCRAPER_TELEGRAM_BOT_TOKEN = os.getenv("HEDGEYE_SCRAPER_TELEGRAM_BOT_TOKEN")
HEDGEYE_SCRAPER_TELEGRAM_GRP = os.getenv("HEDGEYE_SCRAPER_TELEGRAM_GRP")
WS_SERVER_URL = os.getenv("WS_SERVER_URL")

# Load accounts from credentials file
with open("cred/hedgeye_credentials.json", "r") as f:
    accounts = json.load(f)

options = Options()
options.add_argument("--headless")
options.add_argument("--disable-search-engine-choice-screen")
options.add_argument("--disable-extensions")
options.add_argument("--disable-popup-blocking")

last_alert_details = {}


def random_scroll(driver, max_time=30):
    """Perform random scrolling on the page within a maximum time limit."""
    end_time = time.time() + max_time
    while time.time() < end_time:
        scroll_amount = random.randint(-600, 600)
        driver.execute_script(f"window.scrollBy(0, {scroll_amount});")
        time.sleep(random.uniform(0.5, 2))
    driver.execute_script("window.scrollTo(0, 0);")


def login(driver, email, password):
    login_url = "https://accounts.hedgeye.com/users/sign_in"
    driver.get(login_url)
    time.sleep(random.uniform(1, 3))

    retry_count = 0
    while driver.current_url == login_url and retry_count < 5:
        if retry_count > 0:
            log_message(
                f"Login failed for {email}. Retry attempt {retry_count}...",
                "WARNING",
            )

        random_scroll(driver, max_time=10 + retry_count * 5)

        email_input = driver.find_element(By.ID, "user_email")
        email_input.clear()
        email_input.send_keys(email)
        time.sleep(random.uniform(0.5, 1.5))

        password_input = driver.find_element(By.ID, "user_password")
        password_input.clear()
        password_input.send_keys(password)
        time.sleep(random.uniform(0.5, 1.5))

        password_input.send_keys(Keys.RETURN)

        time.sleep(random.uniform(3, 5))
        retry_count += 1

    if driver.current_url == login_url:
        log_message(f"Login failed after 5 attempts for {email}. Aborting.", "ERROR")
        return False
    return True


def add_random_delays():
    """Add random delays between actions."""
    time.sleep(random.uniform(0.5, 2))


def randomize_user_agent(driver):
    """Randomize the user agent string."""
    user_agents = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.1.1 Safari/605.1.15",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/92.0.4515.107 Safari/537.36",
    ]
    driver.execute_cdp_cmd(
        "Network.setUserAgentOverride", {"userAgent": random.choice(user_agents)}
    )


def add_noise_to_actions(driver):
    """Add slight noise to mouse movements and keyboard input."""
    action = ActionChains(driver)
    action.move_by_offset(random.randint(-5, 5), random.randint(-5, 5))
    action.perform()


def fetch_alert_details(session):
    response = session.get("https://app.hedgeye.com/feed_items/all")
    soup = BeautifulSoup(response.text, "html.parser")
    try:
        alert_title = soup.select_one(".article__header")
        if alert_title:
            alert_title = alert_title.get_text(strip=True)
        else:
            return None
    except Exception as e:
        log_message(f"Failed to fetch alert title: {e}", "ERROR")
        return None

    try:
        alert_price = soup.select_one(".currency.se-live-or-close-price")
        if alert_price:
            alert_price = alert_price.get_text(strip=True)
        else:
            return None
    except Exception as e:
        log_message(f"Failed to fetch alert price: {e}", "ERROR")
        return None

    try:
        created_at_utc = soup.select_one("time[datetime]")["datetime"]
    except Exception as e:
        log_message(f"Failed to fetch or parse created_at_utc: {e}", "ERROR")
        return None
    created_at = datetime.fromisoformat(created_at_utc.replace("Z", "+00:00"))
    edt = pytz.timezone("America/New_York")
    created_at_edt = created_at.astimezone(edt)
    current_time_edt = datetime.now(pytz.utc).astimezone(edt)

    return {
        "title": alert_title,
        "price": alert_price,
        "created_at": created_at_edt.strftime("%Y-%m-%d %H:%M:%S %Z%z"),
        "current_time": current_time_edt.strftime("%Y-%m-%d %H:%M:%S %Z%z"),
    }


async def monitor_feeds_async():
    global last_alert_details
    market_is_open = False
    logged_in = False
    first_time_ever = True

    while True:
        pre_market_login_time, market_open_time, market_close_time = (
            get_next_market_times()
        )
        current_time_edt = datetime.now(pytz.timezone("America/New_York"))

        if (
            pre_market_login_time <= current_time_edt < market_open_time
            or first_time_ever
        ):
            first_time_ever = False
            if not logged_in:
                log_message("Logging in...", "INFO")
                sessions = []

                for i, (email, password) in enumerate(accounts):
                    driver = Chrome(options=options)
                    driver.set_page_load_timeout(1200)
                    randomize_user_agent(driver)
                    login(driver, email, password)
                    log_message(f"Logged in with account {i + 1}: {email}", "INFO")
                    session_to_share = Session(driver=driver)
                    session_to_share.transfer_driver_cookies_to_session()
                    sessions.append(session_to_share)
                    driver.quit()

                log_message("All accounts logged in. Starting monitoring...", "INFO")
                logged_in = True

        elif market_open_time <= current_time_edt <= market_close_time:
            if not market_is_open:
                log_message("Market is open, starting monitoring...", "INFO")
                market_is_open = True
            try:
                for session in sessions:
                    alert_details = fetch_alert_details(session)
                    if alert_details is None:
                        log_message("Current alert not interesting to us...", "INFO")
                        await asyncio.sleep(0.7)
                        continue

                    if alert_details["title"] != last_alert_details.get("title"):
                        message = f"Title: {alert_details['title']}\nPrice: {alert_details['price']}\nCreated At: {alert_details['created_at']}\nCurrent Time: {alert_details['current_time']}"
                        await send_telegram_message(
                            message,
                            HEDGEYE_SCRAPER_TELEGRAM_BOT_TOKEN,
                            HEDGEYE_SCRAPER_TELEGRAM_GRP,
                        )

                        signal_type = (
                            "Buy"
                            if "buy" in alert_details["title"].lower()
                            else (
                                "Sell"
                                if "sell" in alert_details["title"].lower()
                                else "None"
                            )
                        )
                        ticker_match = re.search(
                            r"\b([A-Z]{1,5})\b(?=\s*\$)", alert_details["title"]
                        )
                        ticker = ticker_match.group(0) if ticker_match else "-"

                        await send_ws_message(
                            {
                                "name": "Hedgeye",
                                "type": signal_type,
                                "ticker": ticker,
                                "sender": "hedgeye",
                            },
                            WS_SERVER_URL,
                        )

                        log_message(f"New alert sent: {message}", "INFO")
                        last_alert_details = {
                            "title": alert_details["title"],
                            "created_at": alert_details["created_at"],
                        }
                    await asyncio.sleep(0.6)

            except Exception as e:
                log_message(f"Error: {e}", "ERROR")
                await asyncio.sleep(0.7)
        else:
            logged_in = False
            market_is_open = False
            await sleep_until_market_open()


if __name__ == "__main__":
    asyncio.run(monitor_feeds_async())
