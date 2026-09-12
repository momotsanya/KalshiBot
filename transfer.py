# V1.0
"""
Timestamp Enforcement: 
    Kalshi's API enforces strict timeout limits on requests. 
    Generating timestamp_ms dynamically inside the header builder ensures your signature won't expire before reaching Kalshi's servers.
Payload Units: 
    The amount parameter must strictly be an integer passed as cents, not dollars.
Shard Codes: 
    Reference your intended destination appropriately:
    0: Default Index (Macro / Markets)
    1: Shard 1 (Combos)
    2: Shard 2 (Crypto)
    3: Shard 3 (Sports)
"""

import base64
import json
import time
from datetime import datetime
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
import requests

# ==========================================
# CONFIGURATION
# ==========================================
# Base URL for Kalshi Production Trade API
# (Can be switched to 'https://kalshi.co' for testing)
BASE_URL = "https://external-api.kalshi.com" 
ENDPOINT_PATH = "/trade-api/v2/portfolio/intra_exchange_instance_transfer"

# Replace these with your actual Kalshi credentials
API_KEY_ID = "3cf1bd18-592d-4955-aaad-dfd2d627a7b6"
PRIVATE_KEY_PATH = "api.pem"

# ==========================================
# CRYPTOGRAPHIC SIGNING HELPER
# ==========================================
def load_private_key(path):
    with open(path, "rb") as key_file:
        return serialization.load_pem_private_key(
            key_file.read(),
            password=None
        )

def generate_kalshi_headers(method: str, path: str, api_key_id: str, private_key) -> dict:
    """
    Generates Kalshi authentication headers using RSA-PSS SHA-256 signing.
    """
    # 1. Get current epoch timestamp in milliseconds
    timestamp_ms = str(int(time.time() * 1000))
    
    # 2. Construct the exact pre-image string (Timestamp + UPPERCASE_METHOD + Path)
    # Note: If path has query parameters, strip them before signing!
    preimage = f"{timestamp_ms}{method.upper()}{path}"
    
    # 3. Cryptographically sign the message string using RSA-PSS
    signature = private_key.sign(
        preimage.encode('utf-8'),
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH
        ),
        hashes.SHA256()
    )
    
    # 4. Base64 encode the output signature bytes
    base64_signature = base64.b64encode(signature).decode('utf-8')
    
    # 5. Compile required header keys
    return {
        "KALSHI-ACCESS-KEY": api_key_id,
        "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
        "KALSHI-ACCESS-SIGNATURE": base64_signature,
        "Content-Type": "application/json"
    }

# ==========================================
# EXECUTE THE TRANSFER
# ==========================================
def execute_shard_transfer(amount_cents: int, source_shard: int, dest_shard: int):
    """
    Moves funds between Kalshi exchange sharded indices.
    """
    # Load private RSA key
    try:
        private_key = load_private_key(PRIVATE_KEY_PATH)
    except FileNotFoundError:
        print(f"❌ Error: Private key file not found at {PRIVATE_KEY_PATH}")
        return

    # Generate standard cryptographic headers
    headers = generate_kalshi_headers(
        method="POST",
        path=ENDPOINT_PATH,
        api_key_id=API_KEY_ID,
        private_key=private_key
    )

    # Set up JSON payload 
    # Example: Shard 0 (Default/Elections) to Shard 2 (Crypto)
    payload = {
        "source": "event_contract",                
        "destination": "event_contract",
        "amount": amount_cents,                     # Amount must be in cents ($10.00 = 1000)
        "source_exchange_shard": source_shard,       # 0 = Default, 1 = Combos, 2 = Crypto, 3 = Sports
        "destination_exchange_shard": dest_shard,
        "source_subaccount": 0,                     # Optional: defaults to primary subaccount
        "destination_subaccount": 0                 # Optional: defaults to primary subaccount
    }

    full_url = f"{BASE_URL}{ENDPOINT_PATH}"
    print(f"🔄 Initiating transfer of ${amount_cents/100:.2f} from Shard {source_shard} to Shard {dest_shard}...")

    # Make the HTTP POST Request
    response = requests.post(full_url, json=payload, headers=headers)

    # Handle API response
    if response.status_code == 200:
        print("✅ Transfer successfully submitted!")
        try:
            print("Response Data:", json.dumps(response.json(), indent=2))
        except ValueError:
            print("Transfer successful (Empty response returned).")
    else:
        print(f"❌ Transfer Failed (Status Code: {response.status_code})")
        print("Error details:", response.text)

if __name__ == "__main__":
    # Example: Move $1.00 (10000 cents) from Default (Shard 0) to Crypto (Shard 2)
    execute_shard_transfer(amount_cents=100, source_shard=0, dest_shard=2)
