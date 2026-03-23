#!/usr/bin/env bash
set -euo pipefail

echo "==> Comunito Portal V8: instalador base (GitHub)"

ME="$(whoami)"
APP_DIR="/home/$ME/comunito_portal_v8"
VENV="$APP_DIR/comunito-venv"
SVC="/etc/systemd/system/comunito-portal.service"

REPO_URL="https://github.com/comunito/comunito_portal_v8.git"

if [ "$REPO_URL" = "https://github.com/TU_USUARIO/comunito_portal_v8.git" ]; then
  echo "ERROR: edita install/install.sh y reemplaza https://github.com/TU_USUARIO/comunito_portal_v8.git por tu URL real de GitHub"
  exit 1
fi

echo "==> 1) Paquetes base"
sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
  python3-full python3-venv python3-pip \
  libglib2.0-0 libxext6 libsm6 libxrender1 libgl1 \
  build-essential curl ca-certificates git unzip \
  network-manager tzdata iproute2 net-tools \
  gstreamer1.0-tools gstreamer1.0-libav gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
  dnsmasq

sudo systemctl enable NetworkManager --now || true

echo "==> 2) Clonar/actualizar repo"
if [ -d "$APP_DIR/.git" ]; then
  cd "$APP_DIR"
  git pull --rebase
else
  sudo rm -rf "$APP_DIR"
  git clone "$REPO_URL" "$APP_DIR"
fi

echo "==> 3) Crear venv e instalar requirements"
python3 -m venv "$VENV"
# shellcheck disable=SC1091
source "$VENV/bin/activate"
python -m pip install --upgrade pip wheel setuptools
pip install -r "$APP_DIR/requirements.txt"

echo "==> 4) Validar fast_alpr"
python - <<'PYCHK'
import sys
try:
    from fast_alpr import ALPR
    print("[OK] import fast_alpr")
    alpr = ALPR(
        detector_model="yolo-v9-t-384-license-plate-end2end",
        ocr_model="cct-xs-v1-global-model"
    )
    print("[OK] ALPR engine listo")
except Exception as e:
    print("[ERROR] fast_alpr no quedó operativo:", e)
    sys.exit(1)
PYCHK

echo "==> 5) Configurar LAN de cámaras en eth0"
sudo nmcli connection delete comunito-cam-lan 2>/dev/null || true
sudo nmcli connection add \
  type ethernet \
  ifname eth0 \
  con-name comunito-cam-lan \
  ipv4.method manual \
  ipv4.addresses 192.168.88.1/24 \
  ipv6.method ignore \
  autoconnect yes || true
sudo nmcli connection up comunito-cam-lan || true

echo "==> 6) Configurar DHCP para cámaras"
sudo mkdir -p /etc/dnsmasq.d
sudo cp "$APP_DIR/install/network/cam_eth.conf" /etc/dnsmasq.d/cam_eth.conf
sudo systemctl enable dnsmasq
sudo systemctl restart dnsmasq

echo "==> 7) Instalar Tailscale"
curl -fsSL https://tailscale.com/install.sh | sh
sudo systemctl enable tailscaled --now || true
echo "==> Tailscale instalado. Después ejecuta: sudo tailscale up"

echo "==> 8) Crear config_v8.json si no existe"
if [ ! -f "$APP_DIR/config_v8.json" ]; then
  "$VENV/bin/python" - <<'PYCFG'
from app.portal_v8_models import load_cfg, save_cfg, DEFAULT_CFG_FILE
cfg = load_cfg(DEFAULT_CFG_FILE, "config_full.json")
save_cfg(cfg, DEFAULT_CFG_FILE)
print("[OK] config_v8.json generado")
PYCFG
fi

echo "==> 9) Instalar systemd service"
TMP_SVC="/tmp/comunito-portal-v8.service"
cp "$APP_DIR/systemd/comunito-portal-v8.service" "$TMP_SVC"
sed -i "s|__USER__|$ME|g" "$TMP_SVC"
sed -i "s|__APP_DIR__|$APP_DIR|g" "$TMP_SVC"
sudo cp "$TMP_SVC" "$SVC"

echo "==> 10) Habilitar servicio"
sudo systemctl daemon-reload
sudo systemctl enable comunito-portal.service --now

IP_NOW="$(hostname -I | awk '{print $1}')"
echo
echo "==> Listo:"
echo "    Portal:   http://$IP_NOW:5000"
echo "    Settings: http://$IP_NOW:5000/settings"
echo "    Tailscale: ejecuta sudo tailscale up"
