import requests
import time
import hmac
import base64
import hashlib
import json
import os
from dotenv import load_dotenv
import tabulate

class AbstractExchange:
    """An Abstract Exchange Class"""
    
    def __init__(self):
        load_dotenv()

    # GET
    def funding(self):
        """Show active funding on open ordered symbols"""
        raise NotImplementedError

    def orders(self):
        """Show all open orders"""
        raise NotImplementedError
    
    def balance(self):
        """Get balance of account"""
        raise NotImplementedError
    
    def price(self):
        """Get current price"""
        raise NotImplementedError

    # POST
    def limit(self):
        """Open a limit order"""
        raise NotImplementedError
        
    def market(self):
        """Open a market order"""
        raise NotImplementedError
    
    def limit_batch(self):
        """Open a batch of limit orders"""
        raise NotImplementedError
    
    # Delete
    def cancel(self):
        """Cancel Order given <orderid>"""
        raise NotImplementedError
    
    def cancel_all(self):
        """Cancel all open orders from <symbol>"""
        raise NotImplementedError
    
    def nuke_all(self):
        """Nukes all open orders"""
        raise NotImplementedError

class KuCoin(AbstractExchange):
    """KuCoin Futures Exchange Implementation"""
    
    api: str
    secret: str
    url: str
    passphrase: str

    def __init__(self):
        load_dotenv()
        self.api = os.getenv("KuCoinAPI")
        self.secret = os.getenv("KuCoinSecret")
        self.url = "https://api-futures.kucoin.com"
        self.passphrase = os.getenv("KuCoinPass")
        
        if not all([self.api, self.secret, self.passphrase]):
            print(f"Warning: Missing KuCoin credentials - API: {bool(self.api)}, Secret: {bool(self.secret)}, Passphrase: {bool(self.passphrase)}")
    
    def _generate_signature(self, timestamp, method, endpoint, body=None):
        """Generate KuCoin signature for API authentication"""
        if body is None or body == "":
            body_str = ""
        else:
            body_str = json.dumps(body)
            
        what = timestamp + method + endpoint + body_str
        signature = base64.b64encode(
            hmac.new(
                self.secret.encode('utf-8'), 
                what.encode('utf-8'), 
                hashlib.sha256
            ).digest()
        ).decode('utf-8')
        
        return signature
    
    def _get_headers(self, method, endpoint, body=None):
        """Generate authentication headers for KuCoin API requests"""
        timestamp = str(int(time.time() * 1000))
        signature = self._generate_signature(timestamp, method, endpoint, body)
        
        # KC-API-SIGN-PASSPHRASE needs to be encrypted for v2 API
        passphrase = base64.b64encode(
            hmac.new(
                self.secret.encode('utf-8'), 
                self.passphrase.encode('utf-8'), 
                hashlib.sha256
            ).digest()
        ).decode('utf-8')
        
        headers = {
            "KC-API-KEY": self.api,
            "KC-API-SIGN": signature,
            "KC-API-TIMESTAMP": timestamp,
            "KC-API-PASSPHRASE": passphrase,
            "KC-API-KEY-VERSION": "3",
            "Content-Type": "application/json"
        }
        
        return headers
    
    def balance(self) -> tabulate:
        """Get balance of account"""
        endpoint = "/api/v1/account-overview?currency=USDT"
        response = requests.request("GET", f"{self.url}{endpoint}", headers=self._get_headers("GET", endpoint)).json()
        balance_info = response["data"]
        formatted = []
        for key, value in balance_info.items():
            formatted.append({"Property": key, "Value": value})
            
        table = tabulate.tabulate(formatted, headers="keys", tablefmt="fancy_grid")
        return table
    
    def orders(self) -> tabulate:
        """Get open orders"""
        endpoint = "/api/v1/orders?status=active"
        headers = self._get_headers("GET", endpoint)
        response = requests.get(f"{self.url}{endpoint}", headers=headers)
        response = response.json()['data']['items']

        formatted = []
        for item in response:
            temp = {}
            temp["id"] = item['id']
            temp['symbol'] = item['symbol']
            temp['side'] = item['side']
            temp['price'] = item['price']
            temp['size_usd'] = float(item['value'])
            temp['status']=item['status']
            formatted.append(temp)
        
        if len(formatted) > 0:
            table = tabulate.tabulate(formatted, headers="keys", tablefmt="fancy_grid")
            return table
        else:
            table = tabulate.tabulate([{"Orders": "No Orders Active"}], headers="keys", tablefmt="fancy_grid")
            return table
    
    def price(self, symbol: str) -> json:
        """Get market price for a symbol"""
        endpoint = f"/api/v1/mark-price/{symbol}/current"
        response = requests.get(f"{self.url}{endpoint}").json()['data']
        formatted = []
        for key, value in response.items():
            formatted.append({"Property": key, "Value": value})
        
        table = tabulate.tabulate(formatted, headers="keys", tablefmt="fancy_grid")
        return table
    
    def _limit(self, side: str, symbol: str, price: float, size: float, leverage: int) -> json:
        """Create a new order"""
        endpoint = "/api/v1/orders"
        body = {
            "clientOid": str(int(time.time() * 1000)),
            "symbol": str(symbol),
            "type": "limit",
            "price": str(price),
            "valueQty": size,
            "side": side,
            "leverage": leverage
        }
        
        headers = self._get_headers("POST", endpoint, body)
        response = requests.post(f"{self.url}{endpoint}", headers=headers, json=body).json()
        if response['code'] != '200000':
            return {"Order Failed to open": "Order Failed To Open"}
        else:
            return response
        
    def limit(self, side: str, symbol: str, price: float, size: float, leverage: int) -> json:
        """Create a new limit order"""
        endpoint = "/api/v1/orders"
        body = {
            "clientOid": str(int(time.time() * 1000)),
            "symbol": str(symbol),
            "type": "limit",
            "price": str(price),
            "valueQty": size,
            "side": side,
            "leverage": leverage
        }
        
        headers = self._get_headers("POST", endpoint, body)
        response = requests.post(f"{self.url}{endpoint}", headers=headers, json=body).json()
        if response['code'] != '200000':
            table = tabulate.tabulate([{"Order": "Failed"}], headers="keys", tablefmt="fancy_grid")
            return table
        else:
            formatted = []
            for key, value in response['data'].items():
                print(key, value)
                formatted.append({"Property": key, "Value": value})
            table = tabulate.tabulate(formatted, headers="keys", tablefmt="fancy_grid")
            return table


    def limit_batch(self, side: str, symbol: str, price: str, size: float, leverage: int, orders: int, spread: float):
        """Create a batch of open orders"""
        x = float(size) / int(orders)
        y = float(price)
        for _ in range(int(orders)):
            self._limit(side, symbol, y, x, leverage)
            y -= float(spread)

    def limit_x3(self, side: str, symbol: str, price: float, total_size: float, leverage: int, orders: int, spread: float):
        """
        Create a bottom-heavy position size distribution with x3 sizing ratio and variable spread
        
        Args:
            side: "buy" or "sell"
            symbol: Trading pair symbol
            price: Starting price
            total_size: Total position size to distribute
            leverage: Leverage to use
            orders: Number of orders to create
            spread: Initial spread between orders (will decrease for larger positions)
        """
        orders = int(orders)
        price = float(price)
        total_size=float(total_size)
        spread = float(spread)
        successful_orders = 0

        if orders < 3:
            raise ValueError("limit_x3 requires at least 3 orders")
            
        # Calculate position sizes with bottom-heavy distribution
        # Using a ratio based on position number
        ratio_sum = sum(range(1, orders + 1))
        unit_size = total_size / ratio_sum
        
        positions = []
        current_price = float(price)
        
        # If buying, we place larger orders at lower prices
        # If selling, we place larger orders at higher prices
        price_direction = -1 if side == "buy" else 1
        
        # Store the initial spread for reference
        initial_spread = spread
        
        for i in range(orders):
            # For buy orders: start with small size at highest price, end with largest at lowest price
            # For sell orders: start with small size at lowest price, end with largest at highest price
            if side == "buy":
                order_number = i + 1
                # Adjust spread to get tighter as orders get bigger
                # For buys, this means smaller spreads for later (larger) orders
                spread_factor = 1 - ((i / orders) * 0.5)  # Gradually reduce spread by up to 50%
            else:
                order_number = orders - i
                # For sells, this means smaller spreads for earlier (larger) orders
                spread_factor = 1 - (((orders - i - 1) / orders) * 0.5)  # Gradually reduce spread by up to 50%
                
            # Calculate size for this specific order (proportional to its position)
            order_size = unit_size * order_number
            
            # Round size to appropriate precision
            order_size = round(order_size, 4)
            
            # Place the order
            result = self._limit(side, symbol, current_price, order_size, leverage)
            # Check if order was successful
            order_successful = True
            if "Order Failed to open" in result:
                order_successful = False
            elif result.get('code') != '200000' and 'code' in result:
                order_successful = False
            
            # Only count successful orders
            if order_successful:
                successful_orders += 1
            
            positions.append({
                "price": current_price,
                "size": order_size,
                "spread_used": spread * spread_factor,
                "result": result,
                "successful": order_successful
            })
        
            # Adjust price for next order with the adjusted spread
            current_price += price_direction * (spread * spread_factor)
        
        pos = {
            "orders": positions,
            "total_size": total_size,
            "price_range": f"{price} to {positions[-1]['price']}",
            "initial_spread": initial_spread,
            "final_spread": positions[-1]["spread_used"],
            "orders_open": successful_orders  # Only count successful orders
        }

        # Format the order data for tabulation
        formatted_orders = []
        # Temp for looking at orders.
        for order in pos["orders"]:
            status = "Success" if order.get("successful", False) else "Failed"
            order_id = order["result"].get("data", {}).get("orderId", "N/A") if order["result"] and "data" in order["result"] else "N/A"
            
            formatted_orders.append({
                "Price": order["price"],
                "Size": order["size"],
                "Spread": order["spread_used"],
                "Status": status,
                "Order ID": order_id
            })
        # Create a summary table

        summary = [
            {"Property": "Total Size", "Value": pos["total_size"]},
            {"Property": "Price Range", "Value": pos["price_range"]},
            {"Property": "Initial Spread", "Value": pos["initial_spread"]},
            {"Property": "Final Spread", "Value": pos["final_spread"]},
            {"Property": "Orders Open", "Value": pos["orders_open"]},
        ]
        
        # Generate tables using tabulate
        orders_table = tabulate.tabulate(formatted_orders, headers="keys", tablefmt="fancy_grid")
        summary_table = tabulate.tabulate(summary, headers="keys", tablefmt="fancy_grid") 
        return orders_table, summary_table

    def cancel_all(self, symbol: str):
        """Cancel all orders from <symbol>"""
        endpoint = f"/api/v3/orders?symbol={symbol}"
        headers = self._get_headers("DELETE", endpoint)
        response = requests.delete(f'{self.url}{endpoint}', headers=headers).json()['data']
        formatted = []
        cancellation_data = response['cancelledOrderIds']
        i = 0
        for value in cancellation_data:
            formatted.append({"Property": i, "Order Id": value})
            i+=1
                
        table = tabulate.tabulate(formatted, headers="keys", tablefmt="fancy_grid")
        return table
    
    def positions(self, symbol):
        """
            Returns all active positions of <symbol>
            if <symbol> is empty return all active positions
        """
        if symbol != "":
            endpoint = f"/api/v1/positions?symbol={symbol}"
        else:
            endpoint = "/api/v1/positions"
        headers = self._get_headers("GET", endpoint)
        response = requests.get(f"{self.url}{endpoint}", headers=headers)
        response = response.json()['data']
        return response

class ByBit(AbstractExchange):
    """Bybit Futures Exchange Implementation"""
    
    def __init__(self):
        load_dotenv()
        
        self.api = os.getenv("ByBitAPI")
        self.secret = os.getenv("ByBitSecret")
        self.url = "https://api.bybit.com"
        
        if not all([self.api, self.secret]):
            print(f"Warning: Missing ByBit credentials - API: {bool(self.api)}, Secret: {bool(self.secret)}")
    
    def price(self, symbol):
        pass
    
    def orders(self):
        pass
    
    def open_order(self, price, symbol, type_order):
        pass

class Binance(AbstractExchange):
    """Binance Futures Exchange Implementation"""
    
    def __init__(self):
        load_dotenv()
        
        self.api = os.getenv("BinanceAPI")
        self.secret = os.getenv("BinanceSecret")
        self.url = "https://api.binance.com"
        
        # Validate credentials
        if not all([self.api, self.secret]):
            print(f"Warning: Missing Binance credentials - API: {bool(self.api)}, Secret: {bool(self.secret)}")
    
    def price(self, symbol):
        pass
    
    def orders(self):
        pass
    
    def open_order(self, price, symbol, type_order):
        pass

