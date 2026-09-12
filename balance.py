# V1.0

# Configuration
#API_URL = "https://external-api.kalshi.com/trade-api/v2"  # Use https://kalshi.co for demo
#KEY_ID = "2db9f931-a6cf-4cc1-9b96-772e57f439b7"             # From your Kalshi profile
#PRIVATE_KEY_PATH = "api2.pem"

import requests
import time
import base64
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

key_id = "3cf1bd18-592d-4955-aaad-dfd2d627a7b6"
with open("./api.pem", "rb") as f:
    private_key = serialization.load_pem_private_key(f.read(), password=None)

base_url = "https://external-api.kalshi.com/trade-api/v2"

# Build signature for GET /portfolio/balance
ts = str(int(time.time() * 1000))
method = "GET"
path = "/trade-api/v2/portfolio/balance"
message = (ts + method + path).encode("utf-8")
signature = private_key.sign(
    message,
    padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=hashes.SHA256().digest_size),
    hashes.SHA256(),
)
sig_b64 = base64.b64encode(signature).decode("utf-8")

headers = {
    "KALSHI-ACCESS-KEY": key_id,
    "KALSHI-ACCESS-TIMESTAMP": ts,
    "KALSHI-ACCESS-SIGNATURE": sig_b64,
    "Content-Type": "application/json",
}

resp = requests.get(f"{base_url}/portfolio/balance", headers=headers)
print(f"Balance check status: {resp.status_code}")
print(f"Response: {resp.text}")
