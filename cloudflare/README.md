# Despachador de Revendedor WA

El Worker recibe una petición POST autenticada y activa el workflow Python de GitHub Actions. El programador externo recomendado es cron-job.org (gratuito); así no dependemos de los cron de GitHub ni del Cron Trigger de Cloudflare.

## 1. Configurar el Worker en Cloudflare

En Workers & Pages > pumprecios-publicador > Settings > Variables and Secrets, crear un secret llamado `CRON_DISPATCH_SECRET` con una contraseña larga elegida por el usuario. Mantener también `GITHUB_ACTIONS_TOKEN` como secret existente, con permiso Actions: Read and write para este repositorio.

Desplegar el contenido actual de `pumprecios-publicador.js` (copiar el archivo desde este repositorio al editor del Worker y guardar/deploy). El Worker ya no usa Cron Triggers. En Triggers, retirar el antiguo Cron Trigger cuando el nuevo programador externo esté preparado.

Endpoint:
`POST https://pumprecios-publicador.cholo-vic1.workers.dev/run`

Encabezado requerido:
`Authorization: Bearer EL_MISMO_VALOR_DE_CRON_DISPATCH_SECRET`

Un disparo correcto devuelve HTTP 202. Sin el secreto correcto devuelve HTTP 401.

## 2. Programar cron-job.org

Crear una tarea gratuita:
- URL: `https://pumprecios-publicador.cholo-vic1.workers.dev/run`
- Método: `POST`
- Intervalo: cada 5 minutos
- Encabezado HTTP: `Authorization`
- Valor: `Bearer EL_MISMO_VALOR_DE_CRON_DISPATCH_SECRET`

No publicar ni compartir el secreto. Usar exactamente el mismo valor en Cloudflare y cron-job.org.

## 3. Comprobar el funcionamiento

Cada llamada correcta debe crear una ejecución con evento `workflow_dispatch` en GitHub Actions. Revisar que el job finalice con éxito y que una foto nueva en la carpeta de entrada de Drive produzca la propuesta en Telegram.

## 4. Variables de GitHub Actions

El workflow requiere los secrets existentes: `PUBLISHER_TELEGRAM_BOT_TOKEN`, `PUBLISHER_TELEGRAM_CHAT_ID`, `DRIVE_FOLDER_INPUT`, `DRIVE_FOLDER_PROCESSED`, `DRIVE_FOLDER_OUTPUT`, `GOOGLE_CREDENTIALS_JSON`, `GOOGLE_TOKEN_JSON` y al menos una clave de IA (`GEMINI_API_KEY` o `OPENROUTER_API_KEY`).

El Worker no analiza ni edita fotos: solo activa GitHub Actions. El procesamiento real ocurre en Python. No considerar resuelto hasta confirmar una ejecución automática y una propuesta nueva en Telegram.
