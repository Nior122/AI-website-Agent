import getpass

from integrations.telegram import TelegramBotApi
from integrations.http import ProviderError

token = getpass.getpass("Enter bot token (input hidden): ")
api = TelegramBotApi(token)

try:
    updates = api.get_updates(timeout=1)
    print(f"getUpdates succeeded. Updates received: {len(updates)}")
except ProviderError as error:
    print(f"Error code: {error.code}")
    print(f"HTTP status: {error.status}")
    print(f"Retryable: {error.retryable}")
    print(f"Message: {error}")