# Desplegar el webhook en Cloud Run

Se ejecuta en **Cloud Shell** (console.cloud.google.com → icono `>_`), que ya trae `gcloud`.
Proyecto: `telegram-budget-bot-502313`. Región: `us-central1`.

## 0. Antes de empezar
El código v2 debe estar en GitHub (`git push` a `main`). En Cloud Shell:

```bash
git clone https://github.com/<tu-usuario>/telegram-budget-bot.git && cd telegram-budget-bot
gcloud config set project telegram-budget-bot-502313
```

## 1. Habilitar APIs y permisos (una sola vez)
```bash
gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
  secretmanager.googleapis.com artifactregistry.googleapis.com

SA=budget-bot-sheets@telegram-budget-bot-502313.iam.gserviceaccount.com
gcloud projects add-iam-policy-binding telegram-budget-bot-502313 \
  --member="serviceAccount:$SA" --role="roles/secretmanager.secretAccessor"
```

## 2. Secretos
Los valores son los mismos de los *secrets* de GitHub Actions. El de webhook es nuevo (se genera aquí):
```bash
printf '%s' 'PEGA_TELEGRAM_TOKEN'       | gcloud secrets create telegram-token --data-file=-
printf '%s' 'PEGA_GOOGLE_CREDENTIALS_B64' | gcloud secrets create google-credentials-b64 --data-file=-
printf '%s' 'PEGA_ANTHROPIC_API_KEY'    | gcloud secrets create anthropic-api-key --data-file=-

WEBHOOK_SECRET=$(openssl rand -hex 32)
printf '%s' "$WEBHOOK_SECRET" | gcloud secrets create telegram-webhook-secret --data-file=-
echo "$WEBHOOK_SECRET"   # guárdalo: lo necesitas en el paso 5
```

## 3. Desplegar
`--concurrency 1` y `--max-instances 1` son obligatorios: el estado es un solo documento de Firestore y dos requests simultáneos se pisarían.
```bash
gcloud run deploy budget-bot --source . --region us-central1 \
  --allow-unauthenticated --service-account "$SA" \
  --concurrency 1 --max-instances 1 --min-instances 0 --timeout 120 \
  --set-env-vars "TELEGRAM_CHAT_ID=PEGA_CHAT_ID,GOOGLE_SHEETS_ID=PEGA_SHEETS_ID" \
  --set-secrets "TELEGRAM_TOKEN=telegram-token:latest,GOOGLE_CREDENTIALS_B64=google-credentials-b64:latest,ANTHROPIC_API_KEY=anthropic-api-key:latest,TELEGRAM_WEBHOOK_SECRET=telegram-webhook-secret:latest"
```
Al terminar imprime la URL (`https://budget-bot-xxxx-uc.a.run.app`).

## 4. Probar SIN activar Telegram
```bash
URL=https://budget-bot-xxxx-uc.a.run.app
curl $URL/                                   # → {"ok":true}
curl -s -o /dev/null -w "%{http_code}\n" -X POST $URL/webhook \
  -H 'Content-Type: application/json' -d '{}'   # → 403 (sin secreto)
```

## 5. El corte (activar el webhook)
Condición: en Firestore (`budget_bot/estado`) el campo `estado` debe ser `listo` y no debe haber `pendiente`, para no perder una respuesta a medias.

1. Detener el polling: quitar el cron de escucha de `.github/workflows/budget.yml` (Claude lo prepara) y hacer push.
2. Registrar el webhook:
```bash
TOKEN=PEGA_TELEGRAM_TOKEN
curl "https://api.telegram.org/bot$TOKEN/setWebhook" \
  --data-urlencode "url=$URL/webhook" --data-urlencode "secret_token=$WEBHOOK_SECRET"
curl "https://api.telegram.org/bot$TOKEN/getWebhookInfo"   # last_error_message debe estar vacío
```
3. Escribe `saldos` al bot: debe responder al instante.

## Rollback
```bash
curl "https://api.telegram.org/bot$TOKEN/deleteWebhook"
```
Y se restaura el cron de escucha en `budget.yml` (o se corre `modo=escucha` a mano). Telegram no permite webhook y `getUpdates` a la vez.

## Ver logs
```bash
gcloud run services logs read budget-bot --region us-central1 --limit 50
```
