# Développement local

Prérequis : Python 3.9 ou plus récent et une version Node compatible avec le `package-lock.json`.

## Backend

```bash
cd backend
../.venv/bin/uvicorn app.main:app --reload --port 8000
```

L'API est disponible sur `http://127.0.0.1:8000` et sa documentation sur `http://127.0.0.1:8000/docs`.

## Frontend

Dans un second terminal :

```bash
cd frontend
npm run dev
```

L'interface est disponible sur `http://localhost:5173`.
Vite transmet les appels `/api` au backend sur `127.0.0.1:8000`.

Le mode quotidien mono-processus et le launcher macOS sont documentés dans `docs/local-operations.md`.

## Tests

```bash
cd backend
../.venv/bin/pytest
```

Pour une configuration locale, copier `.env.example` en `.env` et adapter les valeurs sans y placer de secrets dans Git.

Pour préparer le build servi par FastAPI en mode quotidien :

```bash
cd frontend
npm ci
npm run build
```
