# Servicio permanente de Pumprecios en Oracle Cloud

Este despliegue mantiene el publicador Python ejecutándose en una máquina virtual Ubuntu de Oracle Cloud, incluso con la PC apagada. No es un proceso de GitHub Actions: el servicio se reinicia automáticamente si falla o se reinicia la VM.

## Antes de iniciar

- Crear una VM Ubuntu en Oracle Cloud Always Free y conectarse por SSH.
- El token de `PUBLISHER_TELEGRAM_BOT_TOKEN` debe pertenecer al bot publicador, no al bot `@PumpreciosAtencionBot` del Worker de atención.
- Tener los archivos OAuth de Google Drive `credentials.json` y `token.json` que ya usa el sistema.
- El token de Google debe incluir los permisos Drive requeridos por el programa.

## Instalar

Desde la VM Ubuntu, como root:

```bash
git clone https://github.com/cholovic1-arch/revendedor-wa-cloud.git /opt/revendedor-wa
cd /opt/revendedor-wa
sudo bash deploy/oracle-install.sh
```

Si el instalador ya clonó el repositorio, ejecutar solo:

```bash
cd /opt/revendedor-wa
sudo bash deploy/oracle-install.sh
```

## Configurar secretos

Para habilitar análisis visual por IA en la nube, configurar al menos una clave (`GEMINI_API_KEY` o `OPENROUTER_API_KEY`) en GitHub Actions Secrets; en Oracle, agregar la misma variable al `.env`. No guardar claves dentro del código.

Editar el archivo privado:

```bash
sudo nano /opt/revendedor-wa/.env
```

Completar las variables de `.env.example` con los mismos valores reales configurados en GitHub Actions. No subir secretos al repositorio ni pegarlos en chats.

Copiar los archivos OAuth desde el equipo que ya tiene la autorización de Drive a la VM en `/opt/revendedor-wa/credentials.json` y `/opt/revendedor-wa/token.json`. Luego ejecutar:

```bash
sudo chown revendedor:revendedor /opt/revendedor-wa/.env /opt/revendedor-wa/credentials.json /opt/revendedor-wa/token.json
sudo chmod 600 /opt/revendedor-wa/.env /opt/revendedor-wa/credentials.json /opt/revendedor-wa/token.json
sudo systemctl enable --now revendedor-wa
sudo systemctl status revendedor-wa --no-pager
```

## Comprobar

Ver los registros en tiempo real:

```bash
sudo journalctl -u revendedor-wa -f
```

El registro debe mostrar que el bot se conectó y que el monitor de Drive está activo. Enviar un mensaje `/start` al bot publicador y después probar con una foto.

Si aparece un error indicando que `getUpdates` no puede usarse mientras hay un webhook configurado, verificar que el token sea el del bot publicador correcto. No borrar el webhook del bot de atención.

## Atención de Facebook e Instagram

El Worker existente `pumprecios-atencionf` es independiente del publicador Python. Conserva su webhook de Facebook y Telegram; no se debe cambiar el token ni el webhook de `@PumpreciosAtencionBot` al desplegar este servicio. La recepción de Instagram requiere que la cuenta profesional, los permisos y las suscripciones de eventos estén efectivamente habilitados en Meta.

## Importante

No desactivar el flujo de GitHub Actions hasta que la VM esté ejecutando el servicio y se haya probado la recepción de fotos. Cuando el servicio permanente esté confirmado, debe quedar un solo consumidor de actualizaciones para el token del bot publicador; dos procesos de `getUpdates` simultáneos se interfieren.
