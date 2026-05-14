import requests


def handle_orders():
    response = requests.get("http://inventory.internal/orders")
    return response.json()
