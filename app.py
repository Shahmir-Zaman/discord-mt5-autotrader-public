import os
import sys
import socket
import logging
import uvicorn

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def is_port_in_use(port: int, host: str = "0.0.0.0") -> bool:
    """Checks if a port is currently in use or cannot be bound."""
    # 1. Quick connect check on loopback interface
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.2)
        if s.connect_ex(("127.0.0.1", port)) == 0:
            return True
            
    # 2. Bind check on the target host interface
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            s.bind((host, port))
            return False
        except OSError:
            return True

def find_available_port(start_port: int = 8000, host: str = "0.0.0.0", max_attempts: int = 100) -> int:
    """Finds the first available port starting from start_port, incrementing by 1 if occupied."""
    for port in range(start_port, start_port + max_attempts):
        if not is_port_in_use(port, host=host):
            return port
    raise RuntimeError(f"No available port found between {start_port} and {start_port + max_attempts - 1}")

def get_requested_port() -> int:
    """Parses requested port from CLI arguments or PORT environment variable."""
    # Check CLI arguments: --port <num>, -p <num>, or first numeric argument
    args = sys.argv[1:]
    for i, arg in enumerate(args):
        if arg in ("--port", "-p") and i + 1 < len(args) and args[i + 1].isdigit():
            return int(args[i + 1])
        if arg.isdigit():
            return int(arg)
            
    # Check PORT environment variable
    env_port = os.environ.get("PORT", "").strip()
    if env_port.isdigit():
        return int(env_port)
        
    return 8000

def start_server():
    requested_port = get_requested_port()
    actual_port = find_available_port(start_port=requested_port, host="0.0.0.0")

    print("=" * 60)
    print("⚡ Starting Discord-MT5 AutoTrader Web Dashboard")
    if actual_port != requested_port:
        print(f"⚠️  Port {requested_port} is already in use by another instance.")
    os.environ["AUTOTRADER_PORT"] = str(actual_port)
    os.environ["AUTOTRADER_INSTANCE"] = f"instance_{actual_port}"

    print(f"👉 Open your browser at: http://localhost:{actual_port}")
    print(f"🏷️  Instance Identity: instance_{actual_port} (Port {actual_port})")
    print("=" * 60)

    uvicorn.run("web.server:app", host="0.0.0.0", port=actual_port, reload=True)

if __name__ == "__main__":
    start_server()
