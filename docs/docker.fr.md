# Lancer l'API HTTP locale dans Docker

[English version](docker.md)

Le `Dockerfile` du dépôt emballe l'[API HTTP locale](bridge-api.md) du daemon
(page en anglais). OpenClaw tourne en JavaScript et le moteur du daemon,
Daemon Core, en Python ; cette API fait le pont entre les deux : OpenClaw l'appelle pour
composer le contexte du daemon, exécuter les outils mémoire et enregistrer
chaque tour. Companion et les procédures Hermes et OpenClaw n'utilisent pas
Docker ; cette image s'adresse aux développeurs qui veulent cette API dans un
conteneur, par exemple pour la tester isolément.

Le conteneur ne lance que l'API. Il ne démarre ni le worker d'après-tour (le
processus d'arrière-plan qui met la mémoire à jour après chaque réponse), ni la
maintenance nocturne ; l'[architecture](architecture.fr.md) décrit ces
composants.

## Construire l'image

Construire depuis un checkout propre. Le commit est transmis à l'image pour
que l'API puisse indiquer la révision qu'elle exécute :

```bash
DAEMON_REVISION="$(git rev-parse HEAD)"
docker build \
  --build-arg DAEMON_CORE_RUNTIME_REVISION="$DAEMON_REVISION" \
  -t daemon-harness:"$DAEMON_REVISION" .
```

## Garder la mémoire hors du dépôt

La mémoire vit dans un répertoire de la machine hôte, monté dans le conteneur.
Pour un test, utiliser une mémoire jetable déjà initialisée, jamais une mémoire
personnelle. Vérifier qu'elle a la structure attendue :

```bash
DAEMON_STATE_DIR=/chemin/isolé/daemon-harness-state
test -d "$DAEMON_STATE_DIR/memory/users"
```

## Démarrer le conteneur

Le conteneur lit ses réglages dans un fichier `.env` (partir de `.env.example`
et de la [référence de configuration](configuration.md), en anglais). La
révision construite dans l'image est transmise à nouveau avec `-e`, pour
qu'une ancienne valeur restée dans `.env` ne puisse pas la remplacer :

```bash
docker run --detach --restart unless-stopped \
  --name daemon-harness \
  --env-file .env \
  -e DAEMON_CORE_RUNTIME_REVISION="$DAEMON_REVISION" \
  -v "$DAEMON_STATE_DIR/memory:/app/memory" \
  -p 127.0.0.1:8000:8000 \
  daemon-harness:"$DAEMON_REVISION"
```

Le port n'est publié que sur `127.0.0.1` : l'API est destinée aux hôtes de la
même machine.

## Vérifier qu'il est prêt

```bash
curl -s http://127.0.0.1:8000/health
curl -s http://127.0.0.1:8000/ready
```

`/health` indique que le processus répond. `/ready` contrôle le runtime, les
chemins et l'attestation du worker et de l'hôte configurés (ce qui tourne
correspond-il à ce qui a été déployé) ; le contrôle de santé de l'image ne passe
que lorsque `/ready` renvoie `"promotable": true`. Un `/ready` dégradé signifie
que le déploiement n'est pas utilisable tel quel : corriger la cause plutôt que
diriger l'hôte vers une autre copie du code. La page
[exploitation et dépannage](operations.md) (en anglais) explique comment lire
ces rapports.
