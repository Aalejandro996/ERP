from werkzeug.middleware.proxy_fix import ProxyFix

from app import create_app

app = create_app()
# Detrás de Render/Nginx: la IP real del cliente viene en X-Forwarded-For (límites de tasa, bitácora, alertas de acceso)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)
