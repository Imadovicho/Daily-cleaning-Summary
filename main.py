import requests
from datetime import datetime, timedelta
import json
import os
from dotenv import load_dotenv

# --- Load .env ---
load_dotenv()
CLIENT_ID = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")

if not CLIENT_ID or not CLIENT_SECRET:
    print("❌ CLIENT_ID or CLIENT_SECRET not found in .env")
    exit(1)

# --- CONFIG ---
TOKEN_FILE = "breezeway_token.json"
BASE_URL = "https://api.breezeway.io"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") 
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID") 

BASE_PROPERTY_URL = f"{BASE_URL}/public/inventory/v1/property"
BASE_RESERVATION_URL = f"{BASE_URL}/public/inventory/v1/reservation"
BASE_TASK_URL = f"{BASE_URL}/public/inventory/v1/task"

# --- Token caching ---
def save_token(token_data):
    expires_at = datetime.now() + timedelta(hours=23)
    data_to_save = {"access_token": token_data, "expires_at": expires_at.isoformat()}
    with open(TOKEN_FILE, "w") as f:
        json.dump(data_to_save, f)

def load_token():
    try:
        with open(TOKEN_FILE, "r") as f:
            data = json.load(f)
        expires_at = datetime.fromisoformat(data["expires_at"])
        if expires_at > datetime.now():
            print("✅ Found valid token in cache.")
            return data["access_token"]
        else:
            print("Token in cache has expired.")
            return None
    except (FileNotFoundError, json.JSONDecodeError):
        return None

def get_breezeway_token():
    cached = load_token()
    if cached:
        return cached
    print("🔑 Requesting new access token...")
    auth_url = f"{BASE_URL}/public/auth/v1/"
    payload = {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    try:
        response = requests.post(auth_url, headers=headers, json=payload)
        response.raise_for_status()
        token = response.json().get("access_token")
        if token:
            save_token(token)
            print("✅ Successfully generated new access token!")
            return token
    except requests.exceptions.RequestException as err:
        print(f"❌ Error during authentication: {err}")
        return None

# --- Telegram sender ---
def send_to_telegram(message: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    response = requests.post(url, data=payload)
    if response.status_code != 200:
        print(f"❌ Failed to send to Telegram: {response.text}")

# --- Fetch properties ---
def fetch_property_map(headers):
    property_map = {}
    page = 1
    limit = 100
    while True:
        url = f"{BASE_PROPERTY_URL}?limit={limit}&page={page}"
        response = requests.get(url, headers=headers)
        if response.status_code != 200:
            break
        for prop in response.json().get("results", []):
            if prop.get("status") == "active":
                property_map[prop.get("id")] = {"name": prop.get("name") or "Unnamed Property"}
        if len(response.json().get("results", [])) < limit:
            break
        page += 1
    return property_map

# --- Fetch reservations ---
def fetch_reservations(date, headers, checkin=True):
    reservations = []
    page = 1
    limit = 100
    while True:
        url = f"{BASE_RESERVATION_URL}?limit={limit}&page={page}"
        if checkin:
            url += f"&checkin_date_ge={date}&checkin_date_le={date}"
        else:
            url += f"&checkout_date_ge={date}&checkout_date_le={date}"
        response = requests.get(url, headers=headers)
        if response.status_code != 200:
            break
        reservations.extend(response.json().get("results", []))
        if len(response.json().get("results", [])) < limit:
            break
        page += 1
    return reservations

# --- Fetch tasks ---
def fetch_tasks(home_id, date, headers):
    all_tasks = []
    page = 1
    limit = 100
    while True:
        url = f"{BASE_TASK_URL}?home_id={home_id}&scheduled_date={date},{date}&limit={limit}&page={page}"
        response = requests.get(url, headers=headers)
        if response.status_code != 200:
            break
        results = response.json().get("results", [])
        housekeeping_tasks = [t for t in results if t.get("type_department") == "housekeeping"]
        all_tasks.extend(housekeeping_tasks)
        if len(results) < limit:
            break
        page += 1
    return all_tasks

# --- Determine check-in cleaning status ---
def get_checkin_cleaning_status(prop_id, headers, last_checkout_date):
    today = datetime.now().strftime("%Y-%m-%d")

    # Step 1: Fetch all housekeeping tasks scheduled after last checkout
    url = f"{BASE_TASK_URL}?home_id={prop_id}&type_department=housekeeping&scheduled_date={last_checkout_date},{today}&limit=100&page=1"
    all_tasks = []
    while url:
        response = requests.get(url, headers=headers)
        if response.status_code != 200:
            break
        data = response.json()
        tasks = data.get("results", [])
        all_tasks.extend(tasks)

        # Pagination
        total_pages = data.get("total_pages", 1)
        current_page = data.get("page", 1)
        if current_page >= total_pages:
            break
        url = f"{BASE_TASK_URL}?home_id={prop_id}&type_department=housekeeping&scheduled_date={last_checkout_date},{today}&limit=100&page={current_page+1}"

    if not all_tasks:
        return "Dirty"

    # Step 2: Call task detail endpoint to get finished_at and assignments
    detailed_tasks = []
    for t in all_tasks:
        task_id = t.get("id")
        if not task_id:
            continue
        resp = requests.get(f"{BASE_TASK_URL}/{task_id}", headers=headers)
        if resp.status_code != 200:
            continue
        detailed_tasks.append(resp.json())

    if not detailed_tasks:
        return "Dirty"

    # Step 3: Pick last task that has finished_at
    completed_tasks = [t for t in detailed_tasks if t.get("finished_at")]
    if not completed_tasks:
        return "Dirty"

    completed_tasks.sort(key=lambda t: t.get("finished_at"), reverse=True)
    last_task = completed_tasks[0]

    task_name = last_task.get("type") or last_task.get("name") or "Unnamed Task"
    cleaner_name = "Unknown cleaner"
    if last_task.get("assignments"):
        cleaner_name = last_task["assignments"][0].get("name") or "Unknown cleaner"
    finished_date = last_task.get("finished_at")[:10] if last_task.get("finished_at") else "Unknown date"

    return f"Ready - {task_name} - Cleaned by {cleaner_name} - {finished_date}"

# --- Fetch yesterday's completed cleanings ---
def fetch_yesterday_cleanings(headers):
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    property_map = fetch_property_map(headers)
    output = [f"\nYesterday’s Cleaning Summary ({yesterday})"]

    for prop_id, prop_info in property_map.items():
        prop_name = prop_info["name"]
        tasks = fetch_tasks(prop_id, yesterday, headers)
        if not tasks:
            continue
        for task in tasks:
            if task.get("type_department") != "housekeeping":
                continue
            task_name = task.get("type") or task.get("name") or "Unnamed Task"
            assignments = task.get("assignments", [])
            if not assignments:
                continue
            for assignment in assignments:
                cleaner_name = assignment.get("name") or "Unknown"
                status = "Completed" if assignment.get("type_task_user_status") == "completed" or task.get("finished_at") else "Not completed"
                output.append(f"- {prop_name} - {task_name} - {cleaner_name} - {status}")
    return "\n".join(output)



# --- MAIN ---
if __name__ == "__main__":
    token = get_breezeway_token()
    if not token:
        print("❌ Cannot proceed without a valid Breezeway token.")
        exit(1)

    HEADERS = {"accept": "application/json", "Authorization": f"JWT {token}"}
    today = datetime.now().strftime("%Y-%m-%d")
    output = [f"Today’s Cleaning Summary ({today})\n"]
    property_map = fetch_property_map(HEADERS)

    # --- Check-ins ---
    output.append("Check-ins today:")
    checkins = fetch_reservations(today, HEADERS, checkin=True)
    if checkins:
        printed = set()
        for res in checkins:
            prop_id = res.get("property_id")
            prop_info = property_map.get(prop_id)
            if not prop_info:
                continue
            prop_name = prop_info["name"]
            if prop_name in printed:
                continue

            # Find last checkout date
            all_checkouts = [c for c in fetch_reservations(today, HEADERS, checkin=False)
                             if c.get("property_id") == prop_id]
            if all_checkouts:
                last_checkout_date = max(c.get("checkout_date") for c in all_checkouts)
            else:
                last_checkout_date = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")

            prop_status = get_checkin_cleaning_status(prop_id, HEADERS, last_checkout_date)
            output.append(f"- {prop_name} - {prop_status}")
            printed.add(prop_name)
    else:
        output.append("No check-ins today.")

    # --- Check-outs & Pending cleanings ---
    output.append("\nCheck-outs today:")
    checkouts = fetch_reservations(today, HEADERS, checkin=False)
    cleaning_map = {}

    for prop_id, prop_info in property_map.items():
        prop_name = prop_info["name"]
        tasks = fetch_tasks(prop_id, today, HEADERS)
        if tasks:
            cleaning_map[prop_name] = []
            for task in tasks:
                task_name = task.get("type") or task.get("name") or "Unnamed Task"
                assignments = task.get("assignments", [])
                if not assignments:
                    cleaning_map[prop_name].append(f"{prop_name} - {task_name} - Not assigned")
                else:
                    for assignment in assignments:
                        cleaner_name = assignment.get("name") or "Not assigned"
                        assignment_status = assignment.get("type_task_user_status") or "Unknown"
                        cleaning_map[prop_name].append(
                            f"{prop_name} - {task_name} - {cleaner_name} - {assignment_status}"
                        )

    checkout_props = set()
    if checkouts:
        for res in checkouts:
            prop_id = res.get("property_id")
            prop_info = property_map.get(prop_id)
            if not prop_info:
                continue
            prop_name = prop_info["name"]
            checkout_props.add(prop_name)
            if prop_name in cleaning_map:
                for c in cleaning_map[prop_name]:
                    output.append(f"- {c}")
            else:
                output.append(f"- {prop_name}")
    else:
        output.append("No check-outs today.")

    # --- Pending cleanings ---
    output.append("\nPending cleanings:")
    has_pending = False
    for prop_name, cleanings in cleaning_map.items():
        if prop_name not in checkout_props:
            for c in cleanings:
                output.append(c)
                has_pending = True
    if not has_pending:
        output.append("No pending cleanings today.")

    # --- Prepare Today's summary ---
final_message = "\n".join(output)

# --- Prepare Yesterday's summary ---
yesterday_message = fetch_yesterday_cleanings(HEADERS)

# --- Combine both messages ---
combined_message = f"{final_message}\n\n{yesterday_message}"

# --- Send single Telegram message ---
print(combined_message)
send_to_telegram(combined_message)

