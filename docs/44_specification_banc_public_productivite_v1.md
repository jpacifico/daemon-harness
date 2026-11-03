# Spécification Du Banc Public De Productivité V1

Statut : version française conservée pour une future piste `fr-FR`

La [spécification anglaise](44_public_productivity_benchmark_v1.md) est la
référence normative et figée du banc public V1. Le présent document est
conservé pour préparer plus tard un corpus français séparé ; il ne gouverne pas
la campagne anglaise et ses résultats ne seront pas agrégés avec elle.

Cette page définit le banc avant d'en connaître les résultats. Une campagne ne
pourra servir d'argument d'adoption que si son protocole a été figé, versionné
et publié avant le premier tour comptabilisé.

## Question Et Portée

Le banc compare, pour Hermes et OpenClaw, l'expérience réellement proposée par
l'hôte seul à celle du même hôte après installation du daemon.

Il doit répondre séparément à quatre questions :

1. le daemon conserve-t-il les capacités productives natives de l'hôte ?
2. améliore-t-il la réussite lorsque le travail dépend d'un échange passé,
   d'une décision révisée ou d'une situation personnelle ?
3. quel est son effet sur le temps nécessaire pour obtenir un résultat correct ?
4. combien de tokens sont consommés pour obtenir ce résultat, entretien différé
   compris lorsqu'il participe à l'épisode ?

Ce n'est ni un classement général de modèles, ni une compétition entre Hermes
et OpenClaw. Les conclusions restent attachées aux versions, au modèle et à
l'environnement réellement mesurés.

## Principes Non Négociables

- Le protocole doit pouvoir constater aussi bien une amélioration qu'une
  dégradation. Une mauvaise réponse, une lenteur ou une surconsommation du
  daemon ne peut pas être retirée parce qu'elle contrarie le résultat attendu.
- Le daemon ne reçoit aucune procédure propre à un scénario, un site ou un
  outil. Chaque hôte reste propriétaire de ses outils natifs.
- Toutes les données sont synthétiques. Aucun dialogue, calendrier, fichier ou
  souvenir personnel ne participe au banc.
- Les bras utilisent des profils, sessions, workspaces, mémoires, caches et
  files indépendants. Aucun canal ou service de production n'est utilisé.
- Le succès primaire est calculé par des vérificateurs déterministes. Un juge
  LLM ou une lecture humaine peut enrichir l'analyse qualitative, jamais
  remplacer la vérité objective du scénario.
- Il n'existe pas de score global opaque. Réussite, latence et tokens restent
  visibles séparément.
- Les résultats bruts exclus, négatifs et atypiques sont conservés avec leur
  motif. Une nouvelle version du banc n'écrase jamais une campagne antérieure.

## Bras Comparés

### A — Hôte Natif

Installation officielle de l'hôte, dans un profil neuf, avec ses réglages et
capacités mémoire officiels de référence. Le modèle, l'effort, les outils
externes et les autorisations sont ceux inscrits dans le manifeste.

### C — Même Hôte Avec Daemon

Même version, même modèle, même effort et mêmes capacités externes, après
installation canonique du plugin daemon. Les différences documentées de
mémoire, Dreaming, skills et outils mémoire font partie de l'effet évalué ;
elles ne sont pas masquées pour rendre les catalogues artificiellement
identiques.

Pour une tâche native sans mémoire, les outils externes doivent néanmoins être
à parité. Pour une tâche longitudinale, les deux bras reçoivent le même
historique **par des messages ordinaires**. Il est interdit de préparer la
mémoire du bras `C` en écrivant directement ses fichiers tout en laissant `A`
vierge.

L'entretien propre à chaque installation est déclenché uniquement par une voie
officielle et observable. Son mode, sa durée et ses tokens sont consignés. Si
un entretien ne termine pas dans la borne annoncée, cela compte comme un
comportement du produit, pas comme un simple incident de banc.

## Matrice V1

La première version contient dix scénarios généraux. Chaque scénario possède
six graines synthétiques de difficulté équivalente.

| Identifiant | Famille | Résultat contrôlé |
| --- | --- | --- |
| `native.files.aggregate` | fichiers | transformer plusieurs sources en un livrable exact sans modifier les entrées |
| `native.tools.extract` | outils | consulter une source synthétique contrôlée et produire un résultat structuré sourcé |
| `continuity.decision` | continuité | retrouver une décision de travail encore valable et l'action interdite |
| `continuity.project_state` | continuité | reprendre un projet au bon jalon, avec son blocage et sa prochaine action |
| `temporal.present_future` | situation | respecter une contrainte présente et un engagement futur proche |
| `temporal.recent_past` | situation | comprendre qu'un événement récent a changé l'état actuel ou la responsabilité |
| `revision.corrected_fact` | correction | préférer une correction confirmée à une ancienne information devenue fausse |
| `uncertainty.missing_evidence` | preuve | ne pas inventer ce que ni la mémoire ni les outils disponibles ne prouvent |
| `longitudinal.delivery` | séquence | mener un petit livrable sur plusieurs sessions sans perdre décisions et corrections |
| `longitudinal.preference` | séquence | réutiliser une préférence de travail confirmée sans la transformer en règle absolue |

Les sources Web ou distantes nécessaires sont remplacées par un serveur de
fixture local déterministe lorsqu'il faut vérifier un outil. Le scénario ne
dicte jamais l'ordre des appels ou la méthode de navigation.

Les deux scénarios `native.*` et `uncertainty.missing_evidence` comportent un
tour évalué sans historique. Les sept autres reçoivent deux à quatre messages
d'historique puis un tour de reprise ; tous les tokens et toutes les durées
depuis le début de l'épisode restent comptés. Une extension séparée pourra
mesurer un cycle nocturne complet, mais elle ne sera pas mélangée au résultat
V1 tant que les deux hôtes n'offrent pas une observation comparable.

## Taille Et Ordonnancement

Le corpus principal représente :

```text
10 scénarios × 6 graines × 2 bras × 2 hôtes = 240 épisodes notés
```

Un épisode peut contenir plusieurs tours. Le manifeste publie donc également
le nombre total de tours et non le seul nombre d'épisodes.

- Chaque épisode utilise une session fournisseur neuve.
- Les deux bras ne partagent jamais un identifiant opaque.
- Une paire reçoit deux fixtures jumelles, de même structure et de même
  difficulté, puis leur attribution est inversée entre les blocs.
- L'ordre des bras et des hôtes est alterné à partir d'une graine publique.
- Les six répétitions sont réparties sur au moins deux plages horaires afin de
  ne pas confondre un état ponctuel du fournisseur avec l'effet du daemon.
- Un passage de mise au point par scénario précède le gel `v1.0.0`. Il est
  marqué `dry-run`, reste public et ne participe jamais au résultat final.

## Mesure De La Réussite

Chaque scénario possède une réponse attendue calculable et un périmètre
d'effets autorisés. Le rapport publie au minimum :

- `success_first_attempt` : résultat correct sans demande de réparation ;
- `success_within_episode` : résultat correct avant la borne de tours ;
- chaque contrôle élémentaire, pas seulement leur agrégat ;
- les fichiers ou actions produits et les effets hors périmètre ;
- les affirmations cachées inventées par un témoin ;
- les appels d'outils, erreurs et reprises.

Une réponse éloquente avec un livrable faux est un échec. Une réponse exacte
obtenue après une reprise reste un succès final, mais pas un succès au premier
essai.

Les observations qualitatives portent séparément sur la clarté, l'utilité et
la naturalité. Elles sont évaluées à l'aveugle sur un échantillon publié et ne
peuvent inverser un contrôle déterministe.

## Mesure De La Latence

Les horloges utilisent une source monotone et les événements bruts restent
horodatés. Chaque épisode publie, lorsque la frontière existe :

- temps mur complet, démarrage jetable compris et hors démarrage ;
- temps jusqu'au premier fragment réellement visible ;
- temps jusqu'au résultat final correct ;
- durée interne rapportée par l'hôte ;
- calcul propre de la composition daemon ;
- durée et nombre des cellules d'outils ;
- temps d'entretien après-tour ou différé, hors chemin visible.

Le premier fragment OpenClaw n'est pas exposé par le lanceur local actuel. La
V1 devra le mesurer à la frontière de streaming dans l'environnement jetable,
sans modifier le code source d'OpenClaw. Tant que cette mesure manque, elle est
marquée `unavailable` et ne peut pas être remplacée par le hook de message final.

Le rapport donne les valeurs individuelles, la médiane, le percentile 90 et
l'intervalle bootstrap à 95 %. Une moyenne seule ou un résultat lent retiré du
graphique est interdit.

Une réponse fausse mais rapide ne doit pas paraître efficace : pour le temps
jusqu'au résultat correct, un épisode sans succès reçoit la borne de timeout
prépubliée. Sa durée réelle reste également disponible séparément.

## Comptabilité Des Tokens

Les tokens sont séparés par propriétaire et par phase :

| Portée | Contenu |
| --- | --- |
| `host.visible` | appels du modèle de réponse, outils inclus |
| `daemon.critical` | éventuels appels internes avant le résultat visible |
| `host.background` | entretien natif attribuable à l'épisode |
| `daemon.background` | après-tour, Dreaming ou REM attribuable à l'épisode |

Pour chaque portée, le schéma conserve séparément :

- entrée non cachée ;
- lecture et écriture de cache ;
- sortie ;
- raisonnement, lorsqu'il est exposé ;
- total rapporté par le fournisseur ou l'hôte.

Chaque nombre porte une qualité de mesure obligatoire :

- `provider_reported` : usage exact retourné appel par appel ;
- `host_reported` : agrégat fourni par l'hôte ;
- `tokenizer_estimated` : estimation par le tokenizer versionné ;
- `unavailable` : information non exposée, jamais remplacée par zéro.

Hermes expose actuellement les usages par appel, y compris raisonnement et
cache. OpenClaw expose un agrégat entrée, sortie et cache ; l'absence d'un champ
de raisonnement reste donc visible. Les appels internes du daemon utilisent
l'usage fournisseur lorsqu'il existe et une estimation explicitement étiquetée
sinon.

Les indicateurs principaux sont le total par épisode, l'écart `C − A`, les
tokens par épisode réussi et les tokens cumulés jusqu'au premier résultat
correct. Les catégories de cache ne sont pas aplaties dans un faux total de
coût. Une estimation monétaire éventuelle utilise une grille de prix datée et
reste secondaire aux tokens bruts.

Le rapport publie la couverture de mesure par portée. Si un appel fournisseur
connu reste `unavailable`, aucune affirmation de consommation totale ou
d'efficacité en tokens n'est autorisée pour la portée concernée ; seuls les
compteurs effectivement observés peuvent être comparés.

## Analyse Et Règles De Communication

Les effets sont publiés par scénario, par famille et par hôte. La combinaison
des deux hôtes n'est qu'une vue complémentaire.

La campagne peut soutenir l'affirmation « le daemon améliore la productivité »
pour un hôte seulement si toutes les conditions suivantes sont satisfaites :

1. aucun échec critique de sécurité, de périmètre ou de vérité ;
2. sur les tâches natives, le bras `C` ne perd pas plus d'un succès sur douze
   par rapport à `A`, et aucun scénario ne présente une régression répétée ;
3. sur les tâches dépendantes de la continuité, le gain de réussite de `C` est
   d'au moins 20 points de pourcentage ;
4. sur les tâches natives, le ratio médian de latence `C/A` ne dépasse pas
   `1,15` et le ratio au percentile 90 ne dépasse pas `1,25` ;
5. le ratio médian de tokens par réussite native ne dépasse pas `1,25`.

Le daemon peut être qualifié de **plus efficace en tokens** uniquement si son
ratio de tokens par épisode réussi est inférieur ou égal à `1,00`. Entre
`1,00` et `1,25`, le rapport parle d'un surcoût borné et montre le gain de
réussite correspondant. Au-delà, aucune formulation d'efficacité en tokens
n'est autorisée, même si la continuité s'améliore.

Ces seuils sont des portes de communication, pas une loi universelle. Le
rapport conserve les tailles d'effet et leurs intervalles d'incertitude. Un
résultat favorable sur une version ne devient pas une promesse pour toutes les
versions futures.

## Exclusions Définies Avant La Campagne

Une exécution peut être exclue seulement pour l'un des motifs suivants :

- mauvais modèle, mauvaise version ou catalogue d'outils non conforme au
  manifeste ;
- fixture corrompue ou vérificateur incapable de calculer la vérité attendue ;
- contamination prouvée entre deux sessions ou accès à une donnée hors fixture ;
- panne du lanceur avant que l'hôte ne reçoive le tour ;
- indisponibilité fournisseur indépendante confirmée par la sonde commune aux
  deux bras.

Un timeout du modèle, une boucle d'outils, une mauvaise réponse, une lenteur ou
une consommation élevée après réception du tour est un **résultat**, pas une
raison d'exclusion.

Toute exclusion reste dans `runs.jsonl` avec son motif, les contrôles qui l'ont
détectée et la nouvelle graine utilisée. La décision est prise sans regarder si
elle favorise `A` ou `C`.

## Reproductibilité Publique

La V1 ne sera pas déclarée publiée tant qu'un tiers ne pourra pas réaliser deux
opérations distinctes.

### Recalcul Hors Ligne

Sans accès à un modèle ni à un secret, le tiers doit pouvoir partir des
résultats bruts anonymisés et régénérer exactement :

- les succès et contrôles élémentaires ;
- les agrégats de latence et de tokens ;
- les exclusions ;
- les tableaux et graphiques du rapport ;
- les sommes de contrôle de tous les artefacts.

Le générateur de rapport hors ligne est déterministe et n'appelle aucun LLM.

### Rejeu Complet

Avec ses propres identifiants fournisseur, le tiers doit pouvoir relancer les
mêmes épisodes dans des profils jetables. Des formulations et latences
identiques ne sont pas exigées d'un modèle distant ; le protocole, les vérités
attendues et le calcul doivent être identiques, et les nouveaux résultats sont
comparés avec leurs intervalles d'incertitude.

Si une version d'hôte ou de modèle n'est plus disponible, le recalcul hors
ligne reste exact mais le nouveau rejeu constitue une campagne de version
différente ; il ne prétend pas reproduire l'environnement disparu.

Le futur répertoire `benchmarks/productivity_v1/` devra contenir :

```text
README.md                 commandes exactes de recalcul et de rejeu
manifest.schema.json      contrat des versions et mesures
manifests/                manifestes publics des campagnes
fixtures/                 générateurs et graines synthétiques
checkers/                 vérités et contrôles déterministes
runner/                   lanceurs Hermes et OpenClaw
report/                   agrégateur hors ligne et gabarit public
```

Chaque manifeste conserve :

- commit et état propre du daemon ;
- version et somme de contrôle des binaires hôtes ;
- modèle, provider, effort et paramètres exposés ;
- système d'exploitation, architecture et versions des runtimes ;
- hash du catalogue d'outils et des blocs de contexte comparés ;
- graine, ordre, bornes de temps et mode d'entretien ;
- provenance de chaque compteur de tokens ;
- variables non secrètes autorisées et secrets explicitement expurgés.

Les traces publiques ne contiennent ni secret, ni chemin personnel, ni système
propriétaire complet d'un hôte. Lorsqu'un contenu ne peut pas être publié, sa
taille et son hash sont conservés pour vérifier la parité sans le révéler.

Les données synthétiques et le code de reproduction vivent dans Git. Les
résultats bruts anonymisés, plus volumineux, sont publiés dans une archive de
release ; son manifeste et ses sommes SHA-256 restent versionnés dans le dépôt.

## Gel, Version Et Publication

1. Implémenter les fixtures, vérificateurs, schémas et le recalcul hors ligne.
2. Exécuter uniquement les `dry-run` pour corriger l'infrastructure et valider
   que chaque vérité est calculable.
3. Faire vérifier le recalcul par une seconde machine ou un tiers.
4. Geler le corpus et le protocole sous le tag `productivity-v1.0.0`.
5. Exécuter les 240 épisodes sans modifier les seuils ni les exclusions.
6. Publier ensemble rapport, données anonymisées, manifestes, exclusions et
   commandes de reproduction.

Toute correction postérieure produit `v1.0.1` ou `v1.1.0` et une campagne
distincte. Le rapport précédent reste accessible.

## Prochaine Étape D'Implémentation

Construire d'abord le squelette public, le schéma de résultat et le recalcul
hors ligne avec **un seul** scénario déterministe de fichiers. Tant que ce
petit trajet ne se recalcule pas à l'identique sur une seconde copie du dépôt,
aucun des 240 épisodes de la campagne V1 ne doit être lancé.
