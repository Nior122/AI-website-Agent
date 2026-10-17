import getpass
import json
import urllib.error
import urllib.request

base_url = input("Render service base URL: ").strip().rstrip("/")
token = getpass.getpass("API token (input hidden): ")

request = urllib.request.Request(
    base_url + "/health",
    headers={
        "Authorization": "Bearer " + token,
        "Accept": "application/json",
    },
)

try:
    with urllib.request.urlopen(request, timeout=120) as response:
        print("HTTP status:", response.status)
        print(response.read().decode())
except urllib.error.HTTPError as error:
    print("HTTP status:", error.code)
    print(error.read().decode())
except Exception as error:
    print(type(error).__name__ + ":", error)