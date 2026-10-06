import os

# Render dynamically sets $PORT (usually 10000)
port = os.environ.get("PORT", "8000")
bind = f"0.0.0.0:{port}"

# Optimize concurrency for Render free tier (512MB RAM)
workers = 2
threads = 2
timeout = 120
accesslog = "-"
errorlog = "-"
