#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/opt/revendedor-wa"
REPO_URL="https://github.com/cholovic1-arch/revendedor-wa-cloud.git"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Ejecutá este instalador como root: sudo bash deploy/oracle-install.sh"
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y git python3 python3-venv python3-pip tesseract-ocr tesseract-ocr-spa tesseract-ocr-eng fonts-dejavu-core

if ! id revendedor >/dev/null 2>&1; then
  useradd --system --create-home --home-dir /opt/revendedor --shell /usr/sbin/nologin revendedor
fi

if [[ ! -d "$APP_DIR/.git" ]]; then
  git clone "$REPO_URL" "$APP_DIR"
else
  git -C "$APP_DIR" pull --ff-only
fi

chown -R revendedor:revendedor "$APP_DIR"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"

if [[ ! -f "$APP_DIR/.env" ]]; then
  install -o revendedor -g revendedor -m 600 "$APP_DIR/.env.example" "$APP_DIR/.env"
  echo
  echo "Se creó $APP_DIR/.env. Hay que completarlo con los secretos reales antes de iniciar el servicio."
fi

if [[ ! -f "$APP_DIR/credentials.json" || ! -f "$APP_DIR/token.json" ]]; then
  echo
  echo "Faltan credentials.json y/o token.json de Google Drive."
  echo "Copialos desde tu equipo a $APP_DIR/ y asigná permisos: chown revendedor:revendedor $APP_DIR/credentials.json $APP_DIR/token.json"
  echo "Luego: chmod 600 $APP_DIR/credentials.json $APP_DIR/token.json"
fi

install -o root -g root -m 644 "$APP_DIR/deploy/revendedor-wa.service" /etc/systemd/system/revendedor-wa.service
systemctl daemon-reload

if [[ -f "$APP_DIR/credentials.json" && -f "$APP_DIR/token.json" ]] && grep -q '^PUBLISHER_TELEGRAM_BOT_TOKEN=.' "$APP_DIR/.env" && grep -q '^PUBLISHER_TELEGRAM_CHAT_ID=.' "$APP_DIR/.env" && grep -q '^DRIVE_FOLDER_INPUT=.' "$APP_DIR/.env" && grep -q '^DRIVE_FOLDER_OUTPUT=.' "$APP_DIR/.env"; then
  systemctl enable --now revendedor-wa
  echo
  echo "Servicio instalado. Estado:"
  systemctl --no-pager --full status revendedor-wa || true
else
  echo
  echo "Instalación de dependencias terminada; el servicio NO se inició porque faltan credenciales/configuración."
  echo "Completá .env y colocá credentials.json/token.json; después ejecutá:"
  echo "sudo systemctl enable --now revendedor-wa"
fi
