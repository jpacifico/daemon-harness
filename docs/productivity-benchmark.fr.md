# Spécification du Public Productivity Benchmark V1

[English version](productivity-benchmark.md)

Version du protocole : `1.0.0`

Statut : gelé pour l'implémentation ; aucune campagne publique notée n'a eu
lieu. Trois des dix scénarios sont implémentés ; voir
[`benchmarks/productivity_v1/`](../benchmarks/productivity_v1/README.md) (en
anglais).

Langue qui fait foi : l'anglais. Cette page est une traduction de la
[spécification anglaise](productivity-benchmark.md) ; en cas d'écart, la
version anglaise l'emporte.

Ce document définit le banc avant que ses résultats soient connus. Une campagne
ne peut soutenir une affirmation publique d'adoption que si son protocole a été
gelé, versionné et publié avant le premier tour noté.

## Question et périmètre

Pour Hermes et OpenClaw, le banc compare l'expérience réelle fournie par l'hôte
natif à celle que fournit le même hôte une fois le daemon installé.

Il répond séparément à quatre questions :

1. Le daemon préserve-t-il les capacités de productivité natives de l'hôte ?
2. Améliore-t-il la réussite des tâches lorsque le travail dépend d'un échange
   passé, d'une décision révisée ou d'un contexte personnel ?
3. Quel est son effet sur le temps nécessaire pour obtenir un résultat
   correct ?
4. Combien de tokens sont consommés pour obtenir ce résultat, y compris la
   maintenance différée lorsqu'elle contribue à l'épisode ?

Ce n'est ni un classement général de modèles, ni un concours entre Hermes et
OpenClaw. Chaque conclusion reste liée aux versions d'hôte, au modèle, au
fournisseur et à l'environnement réellement mesurés.

## Politique de langue

Le corpus noté de la V1 est uniquement en anglais. Cela comprend :

- les messages de l'utilisateur et les historiques de conversation
  synthétiques ;
- le contenu des fixtures et les réponses attendues ;
- les identifiants des vérificateurs et leurs messages d'échec ;
- les manifestes, les instructions de rejeu et les rapports publics ;
- les libellés des graphiques et les champs de résultats lisibles par machine.

L'anglais est fixé avant l'implémentation pour que la langue ne devienne pas
une variable cachée de la comparaison A/C, et pour que des relecteurs tiers
puissent auditer un corpus commun.

Le français est explicitement réservé à une piste `fr-FR` ultérieure. Cette
piste aura sa propre version, ses graines, ses manifestes, ses résultats et son
rapport. Ses résultats ne seront jamais agrégés aux chiffres principaux
anglais. Un message traduit n'est pas traité comme la même exécution notée, car
la langue peut changer le comportement du modèle et des outils.

## Principes non négociables

- Le protocole doit pouvoir détecter aussi bien une amélioration qu'une
  dégradation. Une mauvaise réponse, une latence élevée ou une forte
  consommation de tokens ne peut pas être retirée parce qu'elle contredit le
  résultat attendu.
- Le daemon ne reçoit aucune procédure adaptée à un scénario, un site ou un
  outil. Chaque hôte reste responsable de ses outils natifs.
- Toutes les données sont synthétiques. Aucune conversation, aucun calendrier,
  fichier, mémoire ou identifiant de production personnel ne participe au banc.
- Les bras utilisent des profils, sessions, espaces de travail, mémoires, caches
  et files indépendants. Aucun canal ni service de production n'est utilisé.
- La réussite principale est calculée par des vérificateurs déterministes. Un
  juge LLM ou une relecture humaine peut ajouter une analyse qualitative, mais
  ne peut pas remplacer une vérité de terrain objective.
- Il n'y a pas de score agrégé opaque. Réussite, latence et consommation de
  tokens restent visibles séparément.
- Les résultats bruts exclus, négatifs et atypiques sont conservés avec leur
  raison. Une nouvelle version du banc n'écrase jamais une campagne antérieure.

## Bras comparés

### A — Hôte natif

Une installation officielle de l'hôte dans un profil propre, avec les réglages
de référence de la mémoire et de l'apprentissage natifs. Le modèle, l'effort de
raisonnement, les outils externes et les autorisations sont consignés dans le
manifeste de la campagne.

### C — Même hôte avec le daemon

La même version de l'hôte, le même modèle, le même effort de raisonnement et les
mêmes capacités externes, une fois le plugin canonique du daemon installé. Les
changements documentés de la mémoire, du Dreaming, des skills et des outils
mémoire font partie de l'impact d'installation évalué. Ils ne sont pas masqués
pour rendre les catalogues artificiellement identiques.

Les outils externes doivent rester à parité pour une tâche native qui ne
dépend pas de la mémoire. Pour une tâche longitudinale, les deux bras reçoivent
le même historique sous forme de messages ordinaires de l'utilisateur et de
l'assistant. Il est interdit d'écrire directement la mémoire du bras C en
laissant A vide.

La maintenance propre à chaque installation n'est déclenchée que par un chemin
officiel et observable. Son mode, sa durée et sa consommation de tokens sont
consignés. Si la maintenance ne se termine pas dans sa borne publiée, c'est un
comportement du produit, pas une exclusion automatique pour cause
d'infrastructure.

## Matrice des scénarios de la V1

La V1 contient dix scénarios généraux. Chaque scénario a six graines
synthétiques indépendantes de difficulté équivalente.

| Identifiant | Famille | Résultat contrôlé |
| --- | --- | --- |
| `native.files.aggregate` | fichiers | transformer plusieurs sources en un livrable exact sans modifier les entrées |
| `native.tools.extract` | outils | inspecter une source synthétique contrôlée et produire un résultat structuré et sourcé |
| `continuity.decision` | continuité | retrouver la décision de travail en vigueur et l'opération qui ne doit pas être relancée |
| `continuity.project_state` | continuité | reprendre un projet au bon jalon, au bon blocage et à la bonne prochaine action |
| `temporal.present_future` | contexte | respecter une contrainte actuelle et un engagement proche |
| `temporal.recent_past` | contexte | comprendre qu'un événement récent a changé l'état actuel ou une responsabilité |
| `revision.corrected_fact` | correction | préférer une correction confirmée à un fait plus ancien qui n'est plus vrai |
| `uncertainty.missing_evidence` | preuve | éviter d'inventer une affirmation que ni la mémoire ni les outils disponibles ne soutiennent |
| `longitudinal.delivery` | séquence | terminer un petit livrable sur plusieurs sessions sans perdre décisions et corrections |
| `longitudinal.preference` | séquence | réutiliser une préférence de travail confirmée sans en faire une règle absolue |

Lorsqu'une tâche a besoin d'une source de type Web ou distante, un serveur local
de fixtures déterministe remplace la source publique instable. Le scénario
n'impose jamais l'ordre des appels d'outils ni la méthode de navigation.

Les deux scénarios `native.*` et `uncertainty.missing_evidence` contiennent un
seul tour noté, sans historique préalable. Les sept autres reçoivent deux à
quatre messages d'historique suivis d'un tour de reprise noté. Tous les tokens
et toutes les durées depuis le début de l'épisode restent dans le périmètre.

Une extension séparée pourra mesurer plus tard un cycle complet de maintenance
nocturne. Elle ne sera pas mêlée à la V1 tant que les deux hôtes n'exposeront pas
une frontière d'observation comparable.

## Taille de l'échantillon et ordre

Le corpus principal contient :

```text
10 scénarios × 6 graines × 2 bras × 2 hôtes = 240 épisodes notés
```

Un épisode peut contenir plusieurs tours : le manifeste publie donc aussi le
nombre total de tours.

- Chaque épisode utilise une session neuve chez le fournisseur.
- Les deux bras ne partagent jamais un identifiant opaque.
- Une paire reçoit des fixtures jumelles isomorphes, de même structure et de même
  difficulté ; l'affectation des fixtures est inversée d'un bloc à l'autre.
- L'ordre des bras et des hôtes alterne à partir d'une graine aléatoire
  publique.
- Les six répétitions sont réparties sur au moins deux plages horaires, pour
  qu'un incident passager chez le fournisseur ne soit pas pris pour un effet du
  daemon.
- Une exécution d'ingénierie par scénario a lieu avant la publication du corpus
  noté. Elle est étiquetée `dry-run`, reste publique et ne contribue jamais aux
  résultats de la V1.

## Mesure de la réussite

Chaque scénario a une vérité de terrain calculable et une frontière d'effets
autorisés. Le rapport publie au minimum :

- `success_first_attempt` : résultat correct sans demande de réparation ;
- `success_within_episode` : résultat correct avant la limite de tours de
  l'épisode ;
- chaque vérification individuelle, et pas seulement leur agrégat ;
- les fichiers ou actions produits et tout effet hors périmètre ;
- les affirmations sur une fixture cachée inventées par un bras ;
- les appels d'outils, les erreurs et les nouvelles tentatives.

Une réponse éloquente accompagnée d'un livrable incorrect est un échec. Une
réponse correcte obtenue après une réparation est une réussite finale, mais pas
une réussite au premier essai.

Les observations qualitatives portent sur la clarté, l'utilité et le naturel.
Elles sont relues à l'aveugle sur un échantillon publié et ne peuvent pas
renverser une vérification déterministe.

## Mesure de la latence

Les horloges utilisent une source monotone, et les événements bruts gardent leurs
horodatages. Chaque épisode publie les valeurs suivantes lorsque leur frontière
existe :

- le temps réel complet, avec et sans le démarrage de l'environnement jetable ;
- le temps jusqu'au premier fragment réellement visible par un utilisateur ;
- le temps jusqu'au résultat final correct ;
- la durée interne rapportée par l'hôte ;
- le temps de composition du daemon ;
- le nombre et la durée des cellules d'outils ;
- le temps de l'après-tour ou de la maintenance différée, hors du chemin
  visible.

L'exécuteur local actuel d'OpenClaw n'expose pas le premier fragment diffusé.
La V1 doit tenter de l'observer à la frontière du streaming dans
l'environnement jetable, sans modifier le code source d'OpenClaw. Si la
frontière reste indisponible, le champ vaut `unavailable` ; un hook de message
final ne peut pas servir de substitut.

Le rapport comprend les valeurs individuelles, la médiane, le 90e centile et un
intervalle bootstrap à 95 %. Il est interdit de ne rapporter qu'une moyenne ou
de retirer un résultat lent d'un graphique.

Une réponse rapide mais fausse ne doit pas paraître efficace. Pour le temps
jusqu'au résultat correct, un épisode sans réussite reçoit la borne de délai
publiée à l'avance. Sa durée réelle reste disponible séparément.

## Décompte des tokens

Les tokens sont séparés par propriétaire et par phase :

| Portée | Contenu |
| --- | --- |
| `host.visible` | appels du modèle de réponse, y compris ceux de la boucle d'outils |
| `daemon.critical` | tout appel interne avant le résultat visible |
| `host.background` | maintenance native attribuable à l'épisode |
| `daemon.background` | après-tour, Dreaming ou REM attribuables à l'épisode |

Pour chaque portée, le schéma garde ces catégories séparées :

- entrée hors cache ;
- lectures et écritures de cache ;
- sortie ;
- raisonnement, lorsqu'il est exposé ;
- total rapporté par le fournisseur ou par l'hôte.

Chaque valeur de tokens porte une qualité de mesure obligatoire :

- `provider_reported` : consommation exacte par appel renvoyée par le
  fournisseur ;
- `host_reported` : consommation agrégée renvoyée par l'hôte ;
- `tokenizer_estimated` : estimation par un tokenizer versionné ;
- `unavailable` : information non exposée, jamais remplacée silencieusement par
  zéro.

Hermes expose aujourd'hui la consommation par appel, raisonnement et cache
compris. L'exécuteur OpenClaw lit le dernier instantané cumulé de consommation
dans le journal natif Codex (*rollout*) et compte les instantanés cumulés
distincts comme autant d'appels de modèle ; il ne doit jamais substituer la
consommation du dernier appel, tirée de l'enveloppe de réponse finale, à celle de
l'épisode entier. L'entrée est répartie en valeurs hors cache, lecture de cache
et écriture de cache à partir de cet instantané natif. Un champ manquant doit
rester visible. Les appels internes du daemon utilisent la consommation du
fournisseur lorsqu'elle est disponible, et une estimation explicitement
étiquetée sinon.

Les indicateurs principaux sont le total de tokens par épisode, l'écart `C − A`,
les tokens par épisode réussi, et les tokens cumulés jusqu'au premier résultat
correct. Les catégories de cache ne sont pas aplaties en un coût total
trompeur. Toute estimation monétaire utilise un manifeste de prix daté et reste
secondaire par rapport aux décomptes bruts de tokens.

Le rapport publie la couverture des mesures pour chaque portée. Si un appel
connu au fournisseur reste `unavailable`, aucune affirmation de consommation
totale ou d'efficacité en tokens n'est permise pour cette portée. Seuls les
compteurs observés peuvent être comparés.

## Analyse et seuils de communication

Les effets sont publiés par scénario, par famille et par hôte. Une vue combinée
des hôtes n'est que complémentaire.

Pour un hôte, la campagne ne peut soutenir l'affirmation « le daemon améliore la
productivité » que si toutes ces conditions sont réunies :

1. aucun échec critique de sécurité, de périmètre ou de vérité ;
2. sur les tâches natives, C ne perd pas plus d'une réussite sur douze par
   rapport à A, et aucun scénario ne montre de régression répétée ;
3. sur les tâches qui dépendent de la continuité, C améliore la réussite d'au
   moins 20 points de pourcentage ;
4. sur les tâches natives, le rapport médian de latence C/A vaut au plus `1.15`
   et le rapport au 90e centile au plus `1.25` ;
5. le rapport médian des tokens par réussite sur les tâches natives vaut au plus
   `1.25`.

Le daemon ne peut être décrit comme **plus économe en tokens** que si son
rapport de tokens par réussite vaut au plus `1.00`. Entre `1.00` et `1.25`, le
rapport décrit un surcoût borné et montre le gain de réussite correspondant.
Au-delà de `1.25`, aucun vocabulaire d'économie de tokens n'est permis, même si
la continuité s'améliore.

Ces seuils sont des seuils de communication, pas des lois universelles. Le
rapport conserve les tailles d'effet et les intervalles d'incertitude. Un
résultat favorable pour une version n'est pas une promesse pour toutes les
versions futures.

## Exclusions préenregistrées

Une exécution ne peut être exclue que pour l'une de ces raisons :

- mauvais modèle, mauvaise version de l'hôte, ou catalogue d'outils
  incohérent avec le manifeste ;
- fixture corrompue ou vérificateur incapable de calculer la vérité de terrain ;
- contamination prouvée entre sessions, ou accès à des données extérieures à la
  fixture ;
- panne de l'exécuteur avant que l'hôte reçoive le tour ;
- panne indépendante du fournisseur, confirmée par la sonde de santé A/C
  commune.

Un délai dépassé par le modèle, une boucle d'outils, une réponse incorrecte, une
latence élevée ou une forte consommation de tokens après que l'hôte a reçu le
tour est un **résultat**, pas une raison d'exclusion.

Chaque exclusion reste dans `runs.jsonl` avec sa raison, les vérifications qui
l'ont détectée et la graine de remplacement. La décision est prise sans tenir
compte de ce qu'elle favorise A ou C.

## Reproductibilité publique

La V1 n'est pas considérée comme publiée tant qu'un tiers ne peut pas effectuer
deux opérations distinctes.

### Recalcul hors ligne

Sans modèle ni secret, un relecteur doit pouvoir partir des résultats bruts
anonymisés et régénérer exactement :

- les issues de réussite et les vérifications individuelles ;
- les agrégats de latence et de tokens ;
- les exclusions ;
- les tableaux et graphiques du rapport ;
- les sommes de contrôle de chaque artefact.

Le générateur de rapport hors ligne est déterministe et ne fait aucun appel à un
LLM.

### Rejeu complet

Avec ses propres identifiants chez le fournisseur, un relecteur doit pouvoir
rejouer les mêmes épisodes dans des profils jetables. Une formulation identique
et une latence à la milliseconde près ne sont pas attendues d'un modèle
distant ; le protocole, la vérité de terrain et les calculs doivent être
identiques, et les résultats du rejeu sont comparés avec des intervalles
d'incertitude.

Si une version d'hôte ou de modèle n'est plus disponible, le recalcul hors ligne
reste exact, mais le nouveau rejeu est une campagne d'une autre version. Il ne
prétend pas reproduire l'environnement indisponible.

Le répertoire `benchmarks/productivity_v1/` doit contenir :

```text
README.md                 commandes exactes hors ligne et en direct
manifest.schema.json      contrat de version et de mesure
manifests/                manifestes publics des campagnes
fixtures/                 générateurs synthétiques et graines
checkers/                 vérité de terrain et vérifications déterministes
runner/                   lanceurs Hermes et OpenClaw
report/                   agrégateur hors ligne et modèle de rapport public
```

Chaque manifeste consigne :

- le commit du daemon et l'état propre du worktree ;
- les versions des hôtes et les sommes de contrôle de leurs binaires ;
- le modèle, le fournisseur, l'effort de raisonnement et les paramètres
  exposés ;
- le système d'exploitation, l'architecture et les versions des environnements
  d'exécution ;
- les empreintes des catalogues d'outils et des blocs de contexte comparés ;
- la graine aléatoire, l'ordre, les bornes de délai et le mode de maintenance ;
- la provenance de chaque compteur de tokens ;
- les variables d'environnement non secrètes autorisées, et le masquage
  explicite des secrets.

Les traces publiques ne contiennent aucun secret, aucun chemin personnel ni
aucun prompt système propriétaire complet d'un hôte. Lorsqu'un contenu ne peut
pas être publié, sa taille et son empreinte sont conservées pour vérifier la
parité sans le divulguer.

Les données synthétiques et le code de reproduction vivent dans Git. Les
résultats bruts anonymisés plus volumineux sont publiés comme archive de
release ; leur manifeste et leurs sommes de contrôle SHA-256 restent versionnés
dans le dépôt.

## Gel, versionnement et publication

Ce protocole est gelé en version `1.0.0`. Tout changement normatif des
scénarios, des métriques, des seuils, des règles d'exclusion ou de la politique
de langue exige une nouvelle version du protocole et reste visible dans
l'historique Git.

L'implémentation suit cet ordre :

1. implémenter les fixtures, les vérificateurs, les schémas et le recalcul hors
   ligne ;
2. n'exécuter que des épisodes `dry-run` pour corriger l'infrastructure et
   vérifier que chaque valeur de vérité de terrain est calculable ;
3. vérifier le recalcul hors ligne depuis une seconde machine ou par un tiers ;
4. geler le corpus implémenté sous le tag `productivity-v1.0.0` ;
5. exécuter les 240 épisodes sans changer les seuils ni les exclusions ;
6. publier ensemble le rapport, les données anonymisées, les manifestes, les
   exclusions et les commandes de reproduction.

Une correction ultérieure produit une nouvelle version du protocole et du
corpus, comme `1.0.1` ou `1.1.0`, ainsi qu'une campagne distincte. Le rapport
précédent reste disponible.

Aucun des 240 épisodes notés de la V1 ne peut être exécuté tant que chaque
scénario n'est pas implémenté et que son recalcul hors ligne n'est pas identique
depuis une seconde copie du dépôt. Ce qui est implémenté aujourd'hui est listé
dans [`benchmarks/productivity_v1/README.md`](../benchmarks/productivity_v1/README.md)
(en anglais).
