import os

from twilio.rest import Client
from openai import OpenAI

TWILIO_ACCOUNT_SID = os.environ.get('TWILIO_ACCOUNT_SID')
TWILIO_AUTH_TOKEN = os.environ.get('TWILIO_AUTH_TOKEN')
TWILIO_WHATSAPP_NUMBER = os.environ.get('TWILIO_WHATSAPP_NUMBER')
DEEPSEEK_API_KEY = os.environ.get('DEEPSEEK_API_KEY')

# Backward-compatible aliases used in older code paths.
ACCOUNT_SID = TWILIO_ACCOUNT_SID
AUTH_TOKEN = TWILIO_AUTH_TOKEN

GOOGLE_CALENDAR_CREDENTIALS = {}

twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

deepseek_client = OpenAI(
    api_key=DEEPSEEK_API_KEY,
    base_url="https://api.deepseek.com/v1",
)
