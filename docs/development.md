# Développement local

Prérequis constatés : Python 3.9.6 et Node 25.8.2.

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

## Tests

```bash
cd backend
../.venv/bin/pytest
```

Pour une configuration locale, copier `.env.example` en `.env` et adapter les valeurs sans y placer de secrets dans Git.

