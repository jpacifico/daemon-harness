# Déploiement Isolé

Le dépôt canonique reste inactif pendant la preuve de parité. Les commandes
ci-dessous utilisent une racine d'état explicite et ne doivent jamais viser la
mémoire personnelle pendant un test.

## Image

```bash
DAEMON_REVISION="$(git rev-parse HEAD)"
docker build \
  --build-arg DAEMON_CORE_RUNTIME_REVISION="$DAEMON_REVISION" \
  -t daemon-harness-canonical:"$DAEMON_REVISION" .
```

## État Hors Du Dépôt

```bash
DAEMON_STATE_DIR=/chemin/isolé/daemon-harness-state
test -d "$DAEMON_STATE_DIR/memory/users"
```

Le conteneur reçoit le fichier d'environnement, mais la révision construite
reste prioritaire sur une éventuelle ancienne valeur du `.env` :

```bash
docker run --detach --restart unless-stopped \
  --name daemon-harness-canonical \
  --env-file .env \
  -e DAEMON_CORE_RUNTIME_REVISION="$DAEMON_REVISION" \
  -v "$DAEMON_STATE_DIR/memory:/app/memory" \
  -p 127.0.0.1:8000:8000 \
  daemon-harness-canonical:"$DAEMON_REVISION"
```

Contrôler ensuite `/health` puis `/ready`. Une readiness dégradée interdit la
promotion ; elle ne justifie pas un repli silencieux vers un autre checkout.
