import os
from dotenv import load_dotenv

load_dotenv()

# Bot configuration
BOT_TOKEN = os.getenv('BOT_TOKEN')
ADMIN_CHAT_ID = int(os.getenv('ADMIN_CHAT_ID'))

# WireGuard configuration
WG_SERVER_IP = os.getenv('WG_SERVER_IP', 'YOUR_SERVER_IP')  # Your server's public IP
WG_SERVER_PORT = os.getenv('WG_SERVER_PORT', '51820')
WG_SERVER_PUBLIC_KEY = os.getenv('WG_SERVER_PUBLIC_KEY')  # Content of server-public.key

# IP ranges
WEBSITE_IP_RANGE = "10.8.10.0/24"
PERSONAL_IP_RANGE = "10.8.100.0/24"

# Limits
MAX_PROFILES_PER_USER = 5
