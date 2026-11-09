# Give friends a link. Needs the agent already running (start.ps1) and Cloudflare's free tunnel tool:
#   winget install Cloudflare.cloudflared
# It prints an address like https://something.trycloudflare.com . Friends open it and type their invite code.
# The address changes every time you run this, and stops working when you close the window.
cloudflared tunnel --url http://127.0.0.1:8765
