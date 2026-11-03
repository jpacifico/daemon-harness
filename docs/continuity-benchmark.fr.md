# Continuity Benchmark

[English version](continuity-benchmark.md)

Le Continuity Benchmark pose une seule question : **le daemon fait-il de
Hermes un meilleur assistant personnel d'une conversation à l'autre, sans en
faire un moins bon agent ?** Il compare le même Hermes, avec le même modèle et
les mêmes outils, avec et sans le daemon. Toute affirmation qu'il peut soutenir
est fixée ci-dessous, avant que ses résultats soient connus.

Version du protocole : `1.1.0`. Le corpus et le protocole sont en anglais ; la
version anglaise de cette page fait foi. La version 1.1.0 a été spécifiée le
31 octobre 2026, avant toute exécution de la 1.1.

| Version | Campagne | Code | Rapport | Issue |
| --- | --- | --- | --- | --- |
| 1.0.0 | `continuity-1.0.0-20261031` | tag `continuity-1.0.0` | [rapport](evaluations/2026-10-31_continuity_benchmark_1_0.md) (en anglais) | aucune amélioration affirmable (8 contre 10 sur 24) ; capacités conservées (12/12 des deux côtés) |
| 1.1.0 | `continuity-1.1.0-20261031b` | tag `continuity-1.1.0` | [rapport](evaluations/2026-10-31_continuity_benchmark_1_1.md) (en anglais) | aucune amélioration affirmable (10 contre 12 sur 24) |

Enregistrements des deux campagnes :
[`benchmarks/continuity/records/`](../benchmarks/continuity/records/README.md).

L'unité du banc est le **scénario** : une personne inventée qui parle à son
assistant au fil de plusieurs conversations (voir la
[partie 1](#partie-1--assistant-personnel-où-c-doit-être-meilleur)).

Ce banc ne remplace rien. Le [Public Productivity Benchmark V1](productivity-benchmark.fr.md)
et le [Useful Continuity Benchmark v0.1](../benchmarks/useful_continuity/README.md)
restent tels quels, avec leurs propres campagnes. Il reprend les deux tâches
natives de la V1 et leurs vérificateurs, et tire les leçons de l'UCB v0.1 :
ses cas de contexte utile et ses contre-cas, et son constat que connaître le
contexte n'est pas la même chose qu'en faire un usage utile. Son corpus est
neuf : il n'est donc pas ajusté à des cas déjà vus.

## Version 1.1 : la conversation finale se déroule sur plusieurs tours

La version 1.0 jugeait une seule réponse : celle au message d'ouverture de la
conversation finale. Un assistant qui pose d'abord une question nécessaire ne
pouvait jamais montrer ce qu'il fait de la réponse. La version 1.1 rejoue la
partie assistant personnel de la 1.0 en jugeant la continuité sur plusieurs
tours. Elle change ce qui suit, et rien d'autre.

1. **Les mêmes 24 scénarios.** Les scénarios, leurs messages et leurs critères
   sont ceux de la 1.0, inchangés ; la prochaine étape utile de la 1.0 devient
   `outcome`, jugée à la fin de la conversation finale. Chaque scénario reçoit
   une fiche `person` pour la personne simulée
   (`benchmarks/continuity/corpus.py`). Rien n'a changé dans le daemon ni dans
   Hermes entre les deux versions : réutiliser les scénarios n'ajuste donc rien
   à eux, et la comparaison isole l'effet des tours de la conversation finale.
2. **Conversation finale.** Elle s'ouvre sur le message final du scénario,
   écrit sans rien redire. Après chaque réponse de l'assistant, une **personne
   simulée** répond, jusqu'à trois messages de la personne au total, ou met fin
   à la conversation dès qu'elle a ce qu'elle voulait. Tous les messages de la
   conversation finale vont à la même session Hermes.
3. **La personne simulée** est un appel Claude Opus 5.5 séparé et isolé
   (abonnement Claude, effort `medium`, aucun outil, aucun serveur MCP, aucun
   réglage de projet, aucune session enregistrée, un répertoire de travail
   vide). Elle reçoit les instructions publiées (`simulator_instructions.md`),
   ce que la personne a dit auparavant à l'assistant, la fiche `person` du
   scénario et la conversation finale jusque-là. Elle ne reçoit jamais les
   critères, ni quel système elle a en face d'elle. La fiche `person` dit ce
   que la personne veut et ce qu'elle peut dire si on le lui demande : des
   détails jamais donnés auparavant, qu'il est donc légitime de demander. La
   personne simulée ne répète pas les faits antérieurs sauf si on les lui
   demande, n'ajoute jamais un fait absent de sa fiche et peut corriger
   l'assistant. Chaque réponse brute est conservée.
4. **Jugement d'ensemble.** Les juges lisent les messages antérieurs et les
   deux versions de la conversation finale. Par scénario et par système :
   - **Se souvient** (*Remembers*) : chaque fait antérieur est utilisé avant
     que la personne le redise ;
   - **Redemande** (*Asks again*), **Périmé** (*Outdated*), **Invente**
     (*Invents*) : dans n'importe quelle réponse de l'assistant ;
   - **Aide concrète à la fin** : l'aide que décrit le scénario est là à la fin
     de la conversation finale. Une question dont les messages de la personne
     ne contenaient pas la réponse n'est pas une faute ;
   - **Réponse qui apporte d'abord cette aide** : son numéro dans la
     conversation finale (1 pour la première réponse) ; rapporté comme une
     médiane sur les scénarios où l'aide est apportée, en retenant pour chaque
     scénario le plus tardif des numéros des deux juges.
   Un scénario est réussi lorsqu'il se souvient, ne redemande rien, n'applique
   rien de périmé, n'invente rien et apporte l'aide à la fin.
5. **Les capacités d'agent ne sont pas rejouées.** La version 1.0 les a
   mesurées avec le même Hermes, le même modèle et le même daemon (12 tâches
   natives sur 12 des deux côtés). Le rapport 1.1 ne les présente pas comme son
   propre résultat.
6. **Seuil d'affirmation.** Inchangé : « améliore » exige un écart C − A d'au
   moins 6 scénarios réussis sur 24, un intervalle bootstrap apparié à 95 % qui
   exclut zéro, Invente et Périmé non supérieurs pour C, et C préféré plus
   souvent. La règle sur les tokens est inchangée.
7. **Porte d'ingénierie.** Avant les épisodes notés, un scénario d'ingénierie
   par système doit aboutir avec la voix canonique, chaque après-tour terminé,
   aucun token du daemon avant la réponse, et au moins une réponse de la
   personne simulée, message ou fin de conversation.

Inchangés : les deux systèmes et leurs versions, le modèle et son effort, le
profil Companion neuf du daemon, les sessions et l'isolement, l'aveugle, les
deux juges et les deux ordres de présentation, la règle selon laquelle un
critère n'est validé que si les deux juges sont d'accord, et les mesures de
temps et de tokens du tour complet des deux côtés.

Limites connues de la 1.1, énoncées avant l'exécution : la personne simulée et
les juges sont de la même famille de modèles, Claude Opus 5.5 ; les scénarios et
les fiches `person` ont aussi été écrits par Claude Opus 5.5 ; la conversation
finale est plafonnée à trois messages de la personne ; les scénarios ont déjà
été vus en 1.0.

## Ce qui est comparé

Les deux bras exécutent Hermes `0.21.5` (`f97608f`), la version qu'installe
Companion, dans une installation neuve, séparée et jetable. Aucun bras n'utilise
une installation, un profil, une mémoire, un canal ou un stockage d'identifiants
existants.

| | A — Hermes seul | C — Hermes avec le daemon |
| --- | --- | --- |
| Modèle de la voix | abonnement ChatGPT, `gpt-6-astra`, raisonnement `medium` | identique |
| Mémoire | mémoire native de Hermes, profil utilisateur, recherche de sessions, apprentissage de skills et revue d'arrière-plan, tous activés | le daemon, installé avec les réglages qu'applique Companion (mémoire native, profil, incitations aux skills et revue d'arrière-plan désactivés) |
| Appels internes du daemon | aucun | `gpt-6-astra`, valeurs par défaut de Companion : routage `medium`, conversation `medium`, mémoire `high` |
| Outils | outils de la ligne de commande Hermes | même catalogue, vérifié par empreinte |
| Autres réglages de l'hôte | valeurs par défaut de Hermes, raisonnement de l'agent `medium` | identiques |

Dans C, chaque épisode démarre comme un daemon Companion neuf : un coffre
mémoire vide, les modèles de voix fixes de Companion, l'identité de daemon
« Aster » (féminine) et le profil de continuité `parga-v1` que Companion écrit
pour les nouvelles installations.

C'est la configuration que recommande Companion (ChatGPT avec Astra). Le daemon
est la révision de `main` nommée dans le manifeste de la campagne. Rien dans
aucun des bras n'est réglé pour un cas : le daemon ne reçoit aucune procédure
propre à un scénario, et aucun fait mémorisé n'est écrit à la main. Chaque bras
construit sa mémoire uniquement à partir des mêmes messages ordinaires de
l'utilisateur.

### Différences connues avec une installation réelle

- La conversation passe par la ligne de commande Hermes, pas par Telegram :
  chaque portée de plateforme, y compris la valeur implicite « telegram » de la
  voix canonique, nomme aussi la ligne de commande pour que les mêmes facultés
  s'appliquent. L'exécution d'ingénierie vérifie que la voix canonique a bien
  répondu et que le daemon n'a fait aucun appel avant la réponse.
- Les messages de suivi sont désactivés : il n'y a pas de canal pour les
  délivrer.
- Le Dreaming nocturne et le serveur local de recherche par embeddings ne
  tournent pas. La mémoire d'un scénario ne contient que ce que ses quatre
  conversations ont créé ; l'effet éventuel de la sélection par embeddings à
  cette taille n'est pas mesuré.
- Il n'y a pas d'accueil de première rencontre : la personne commence par son
  propre sujet.

## Deux parties

### Partie 1 — Assistant personnel (où C doit être meilleur)

Vingt-quatre **scénarios** synthétiques. Un scénario est une personne inventée
qui parle à son assistant au fil de quatre conversations séparées (quatre
nouvelles sessions Hermes) :

1. **Raconter.** Deux ou trois messages ordinaires qui donnent des faits :
   personnes, contraintes, projets, préférences, inquiétudes.
2. **Ailleurs.** Trois ou quatre messages sur des sujets quotidiens sans
   rapport, assez longs pour que les faits sortent de la fenêtre verbatim de la
   voix (environ 6 000 caractères de dialogue).
3. **Changement.** Un ou deux messages qui mettent la situation à jour : une
   correction, une annulation, une tâche faite, une nouvelle date. Certains
   scénarios n'ont pas de changement.
4. **Demander.** Un message noté, écrit comme le ferait une personne, sans
   redire ce qu'elle a dit auparavant.

Les deux bras reçoivent exactement les mêmes messages de l'utilisateur, dans le
même ordre ; leurs réponses viennent de leurs propres chemins normaux. Toutes
les sessions d'un scénario se déroulent dans une même journée calendaire, pour
que « demain » ou « vendredi » garde un seul sens ; la campagne ne simule pas le
passage des jours.

Six familles de quatre scénarios : trois où le contexte antérieur doit être
utilisé, et un **contre-cas** où le contexte antérieur ne doit **pas** être
appliqué.

| Famille | Ce que la réponse notée doit montrer |
| --- | --- |
| Personnes et contraintes | utilise les personnes concernées et leurs contraintes sans redemander |
| Travail en cours | reprend au bon endroit : ce qui reste à faire, ce qui est réglé |
| Temps et engagements | respecte un engagement à venir, une exception ponctuelle à une routine, un changement récent |
| Corrections | utilise le dernier fait corrigé, pas le précédent |
| Ne pas inventer | dit qu'elle ne sait pas une chose qu'on ne lui a jamais dite, au lieu de l'inventer |
| Relation | revient, brièvement et à propos, sur une chose qui compte pour la personne |

Les contre-cas couvrent par exemple une contrainte qui concernait quelqu'un qui
est parti, une décision qui valait pour un autre projet, ou une inquiétude
résolue qui ne doit pas être rouverte.

### Partie 2 — Capacités d'agent (où C ne doit pas être moins bon)

Les deux tâches natives implémentées de la V1, `native.files.aggregate` et
`native.tools.extract`, avec six graines chacune, un épisode neuf par graine et
par bras. Elles n'ont pas besoin de mémoire. Leurs vérificateurs sont
déterministes et déjà validés : un livrable exact, des entrées inchangées, un
accès réel à chaque source contrôlée.

## Mesures

Chaque mesure est un décompte suivant une règle fixe, publié par bras. Il n'y a
pas de score combiné.

### Mesures de la partie 1, par réponse notée

Chaque scénario est accompagné de critères écrits, fixés dans le corpus avant
toute exécution :

- **must use** (à utiliser) : les faits antérieurs sur lesquels la réponse doit
  s'appuyer ;
- **must not** (à ne pas faire) : ce que la réponse ne doit pas faire
  (appliquer un contexte périmé, rouvrir un point réglé, recommander de refaire
  un travail terminé) ;
- **no repeat** (sans redemande) : ce que la réponse ne doit pas redemander ;
- **unknown** (inconnu) : pour les scénarios « Ne pas inventer », le fait qui
  n'a jamais été donné.

Ce qui en découle :

| Mesure | Règle |
| --- | --- |
| **Se souvient** | chaque fait « must use » est utilisé |
| **Redemande** | nombre de questions sur une chose que la personne a déjà dite |
| **Contexte périmé ou sans rapport** | nombre de réponses qui font une chose listée sous « must not » |
| **Invente** | nombre de réponses qui présentent comme connu un fait jamais donné |
| **Prochaine étape utile** | la réponse donne une action ou une réponse concrète que la personne peut utiliser tout de suite, sauf si le cas exige d'abord une question |
| **Scénario réussi** | Se souvient, aucune redemande, rien de « must not », rien d'inventé, prochaine étape utile |
| **Préférée** | dans une comparaison côte à côte à l'aveugle, quelle réponse ressemble le plus à un assistant qui connaît cette personne et l'aide |

### Mesures de la partie 2

| Mesure | Règle |
| --- | --- |
| **Tâche native réussie** | le vérificateur déterministe passe au premier essai |
| **Erreurs d'outils** | appels d'outils échoués, tels que rapportés par l'hôte |

### Temps et tokens, pour les deux parties

- le temps de réponse de chaque message noté, de son envoi au texte final ;
- le temps jusqu'au premier texte visible, lorsque la trace de l'hôte l'expose ;
- le travail après la réponse, des deux côtés, chronométré séparément : la revue
  d'arrière-plan propre à Hermes dans A (l'entretien de sa mémoire native et de
  ses skills) et l'après-tour du daemon dans C. La personne ne l'attend pas, mais
  il fait partie de ce que coûte chaque côté : le **tour complet** (réponse plus
  travail d'après-tour) est donc rapporté pour les deux ;
- les tokens par scénario et par épisode natif, répartis entre la réponse (les
  appels visibles de l'hôte), les appels du daemon avant la réponse (attendu :
  aucun) et le travail d'après-tour (la revue d'arrière-plan de Hermes, plus
  l'après-tour du daemon dans C), chacun avec sa source (`provider_reported`,
  `host_reported`, `tokenizer_estimated` ou `unavailable`). Un décompte manquant
  n'est jamais zéro.

## Qui juge

Ni les personnes ou agents qui ont construit le daemon, ni l'auteur de ce
protocole.

1. **Le code d'abord.** Les tâches natives sont vérifiées par le code seul. Dans
   la partie 1, chaque verdict qui dit qu'une réponse a fait ou n'a pas fait
   quelque chose doit citer un extrait exact de cette réponse ; le code vérifie
   que l'extrait y figure mot pour mot. Un verdict dont l'extrait est introuvable
   compte comme non vérifié, et un « réussi » non vérifié ne compte pas.
2. **Un juge à l'aveugle d'un autre fournisseur.** Les deux bras répondent avec
   un modèle OpenAI : un modèle OpenAI n'est donc pas utilisé pour les juger. Le
   juge est Claude Opus 5.5 (Anthropic), via un abonnement Claude (sans clé
   API). Chaque appel est une exécution `claude -p` sans outil, sans serveur
   MCP, sans réglage de projet et dans un répertoire de travail vide ; il ne
   reçoit que les instructions publiées du juge et un lot de paquets anonymisés.
   Claude Code ajoute lui-même ses informations d'environnement standard
   (système d'exploitation, date, adresse du compte), qui ne disent rien des
   bras. Un script remplace les noms des bras par des étiquettes aléatoires
   (`X`, `Y`) et mélange les scénarios ; la clé des étiquettes n'est ouverte
   qu'après l'enregistrement de tous les verdicts. Chaque scénario est jugé par
   deux appels séparés ; un critère n'est validé que si les deux sont d'accord,
   et les désaccords sont publiés. La commande exacte est dans
   `benchmarks/continuity/README.md`.
3. **Préférence à l'aveugle.** Chaque agent juge compare deux fois les deux
   réponses notées d'un scénario, dans les deux ordres, sans savoir laquelle est
   laquelle. Une préférence ne compte que si elle résiste à l'inversion ; sinon
   c'est une égalité.
Le jugement est entièrement automatique : aucune personne ne note ni ne relit
une réponse. Ses garde-fous sont les vérifications par le code, l'aveugle, deux
agents juges indépendants aux instructions publiées, et l'inversion de l'ordre
de présentation. L'accord entre les deux juges est publié, et chaque désaccord
est listé.

**Aperçu.** Les scénarios terminés vers le milieu de la campagne peuvent être
jugés une fois à titre d'aperçu, dans un répertoire séparé avec sa propre clé
d'étiquettes. L'aperçu ne change rien à la campagne et ne compte jamais : le
jugement final tire de nouvelles étiquettes et juge tous les scénarios à neuf.

## Tableau des résultats

Le rapport s'ouvre sur ce tableau, rempli exactement, suivi d'une ligne par
famille et des conversations complètes, côte à côte, de chaque scénario.

| | Hermes seul | Hermes + daemon |
| --- | ---: | ---: |
| **Assistant personnel, 24 scénarios** | | |
| Scénarios réussis | n / 24 | n / 24 |
| Se souvient de ce qui a été dit auparavant | n / N | n / N |
| Questions sur une chose déjà dite | n | n |
| Utilise un contexte périmé ou sans rapport | n | n |
| Invente un fait | n | n |
| Donne une prochaine étape utile | n / 24 | n / 24 |
| Préférée dans la comparaison à l'aveugle | n | n (égalités : n) |
| **Capacités d'agent, 12 tâches** | | |
| Tâches natives réussies | n / 12 | n / 12 |
| Erreurs d'outils | n | n |
| **Temps et coût** | | |
| Temps de réponse par message, médiane (90e centile) | s (s) | s (s) |
| Travail d'après-tour par message, médiane | s | s |
| Tour complet par message, médiane (90e centile) | s (s) | s (s) |
| Tokens par scénario, médiane : réponse / après-tour | n / n | n / n |
| Tokens par scénario, médiane : tour complet | n | n |
| Tokens, campagne entière : rapport C / A | 1 | r |

Chaque dénominateur est le nombre de scénarios auxquels la ligne s'applique,
fixé dans le corpus (par exemple, « Se souvient » ne compte que les scénarios
qui ont des faits antérieurs à utiliser). Chaque nombre renvoie à ses scénarios.

## Ce qui peut être dit publiquement

Les résultats du banc sont publiés quels qu'ils soient. Ce qu'ils permettent
d'affirmer dépend de règles fixées dès maintenant.

**« Le daemon améliore Hermes comme assistant personnel »** seulement si toutes
ces conditions sont réunies :

1. Scénarios réussis : C − A vaut au moins 6 sur 24, et l'intervalle bootstrap
   apparié à 95 % de la différence exclut zéro ;
2. Invente : C n'est pas supérieur à A ;
3. Utilise un contexte périmé ou sans rapport : C n'est pas supérieur à A ;
4. Préférée : C est préféré dans plus de scénarios que A.

**« Il conserve les capacités de Hermes »** seulement si toutes ces conditions
sont réunies :

1. Tâches natives réussies : C perd au plus une tâche sur douze par rapport à A,
   et aucune tâche n'échoue sur les six graines pour C tout en réussissant pour
   A ;
2. Temps de réponse sur les tâches natives : rapport médian C/A d'au plus
   `1.15`.

**Vocabulaire sur les tokens.** Le rapport indique le rapport de tokens C/A sur
le tour complet, travail d'après-tour compris des deux côtés, pour toute la
campagne et par scénario. Des mots comme « efficace » ou « économique » ne sont
permis que si ce rapport vaut au plus `1.00` ; sinon le rapport montre le
surcoût et ce qu'il apporte.

Lorsqu'une règle n'est pas satisfaite, le rapport dit laquelle, et
l'affirmation n'est pas faite.

## Exécutions, ordre et exclusions

- Un épisode par scénario et par bras, et un par graine native et par bras :
  48 épisodes de scénario et 24 épisodes natifs, plus un scénario d'ingénierie
  non noté et une tâche native d'ingénierie par bras, publiés comme tels.
- L'ordre des bras alterne à partir d'une graine aléatoire publique ; les
  familles sont entrelacées.
- Chaque épisode démarre avec une mémoire vide, un profil neuf et des sessions
  neuves. Les bras ne partagent jamais un répertoire, un cache, une session ou un
  identifiant.
- La campagne est répartie sur au moins deux plages horaires.
- Aucun nouveau tirage. Une mauvaise réponse, une réponse lente, une boucle
  d'outils ou un nombre élevé de tokens après que l'hôte a reçu le message est un
  **résultat**.
- Un épisode ne peut être exclu que pour : un mauvais modèle ou une mauvaise
  version de l'hôte ; une fixture corrompue ; une contamination prouvée entre
  épisodes ; une panne de l'exécuteur avant que l'hôte reçoive le premier
  message ; une panne du fournisseur confirmée par un contrôle de santé exécuté
  sur les deux bras. Chaque exclusion reste dans les enregistrements publiés avec
  sa raison et son remplacement.

## Isolement et identifiants

Le banc ne touche jamais une installation personnelle : aucun Hermes, daemon
Companion, mémoire ou canal personnel. Hermes est installé par l'installateur
de Companion lui-même, dans un répertoire dédié au banc. La voix et les appels
internes du daemon utilisent deux connexions ChatGPT distinctes créées pour le
banc, car les jetons de rafraîchissement ChatGPT tournent et ne doivent pas être
partagés entre programmes. Les épisodes s'exécutent un par un ; chacun utilise
une copie de sa connexion et rend la copie rafraîchie au stockage du banc à la
fin. Aucun identifiant, chemin personnel ou prompt système de l'hôte n'est
publié.

## Publication et reproductibilité

Les scénarios sont aussi publiés comme un jeu de données réutilisable au format
Hugging Face, `benchmarks/continuity/dataset/` : une ligne JSON par scénario avec
ses conversations et ses critères, la liste des tâches natives, les instructions
du juge et un manifeste de sommes de contrôle. Le jeu de données est généré à
partir du corpus préenregistré et vérifié par rapport à lui : les deux ne
peuvent jamais diverger.

Toutes les conversations sont synthétiques : tout est donc publié. Le corpus,
les critères, chaque message de l'utilisateur et chaque réponse des deux bras,
les verdicts des deux juges avec leurs extraits, les temps, les décomptes de
tokens, les exclusions, le manifeste (révision du daemon, version et somme de
contrôle de Hermes, modèle, efforts de raisonnement, version de Codex CLI,
empreintes des catalogues d'outils, graine aléatoire) et le rapport.

Les enregistrements sont copiés pour publication par
`benchmarks/continuity/export_records.py` : le plan, les exécutions, les
enregistrements et preuves des épisodes, les réponses brutes de la personne
simulée, les paquets anonymisés, les réponses brutes des juges, les verdicts,
les clés d'aveugle et les rapports. Chaque répertoire d'exécution est laissé de
côté (répertoires Hermes et Codex, connexions, journaux, caches et coffre du
daemon de banc), le répertoire personnel local est écrit `~`, et l'export est
refusé s'il reste un motif d'identifiant ou de chemin personnel. Les
enregistrements gardent les noms de champs du code qui les a écrits : ce code
appelait un scénario une « life » (`LIVES`, `lives.jsonl`). Le renommage
ultérieur n'a changé aucun scénario, message, critère ou instruction, et le
fichier de scénarios publié est identique octet pour octet. Les enregistrements
se recalculent avec le tag de leur version. Les enregistrements des campagnes de
référence 1.0 et 1.1 sont dans `benchmarks/continuity/records/`.

- **Recalcul hors ligne :** à partir des enregistrements publiés, un script sans
  appel de modèle régénère chaque nombre du rapport et ses sommes de contrôle.
- **Rejeu complet :** avec ses propres accès ChatGPT et Claude, n'importe qui
  peut rejouer la campagne dans des profils jetables, et le jugement avec les
  instructions publiées du juge. La formulation et les temps différeront ; le
  protocole, les critères et les calculs sont identiques.

## Ordre des travaux

1. Cette spécification et le corpus avec ses critères sont versionnés ensemble
   avant tout appel de modèle ; la somme de contrôle du fichier de corpus est
   enregistrée.
2. Exécuteur, juges et rapport hors ligne, testés sur des données synthétiques.
3. Un scénario d'ingénierie et une tâche native d'ingénierie par bras. Ils ne
   vérifient que le chemin de mesure et sont publiés comme non notés.
4. La campagne notée, sans changer le corpus, les critères ni les règles.
5. Le rapport, avec tous les enregistrements, ajouté à `docs/evaluations/` et
   indexé.

Un changement du corpus, des critères ou des règles après l'étape 1 crée une
nouvelle version du protocole et une nouvelle campagne ; les précédentes restent
publiées.

## Amendements et déclarations

Les changements apportés à ce protocole après sa première rédaction, et les
exécutions qui ne comptent pas, sont listés ici pour que le lecteur puisse juger
de leur effet.

- **Relecture humaine retirée, avant tout verdict (31 octobre 2026).** La
  première rédaction ajoutait une relecture à l'aveugle par des personnes. À la
  demande du mainteneur du projet, elle a été retirée ; le jugement est le
  jugement automatique décrit dans [Qui juge](#qui-juge).
- **Instructions du juge corrigées, après les verdicts de l'aperçu (31 octobre
  2026).** Les instructions du juge ne portaient pas la règle que ce protocole
  avait fixée avant tout épisode, « sauf si le cas exige d'abord une question »,
  pour la prochaine étape utile. Elles disent désormais qu'une question compte
  comme prochaine étape utile lorsque la réponse ne pouvait raisonnablement pas
  agir sans sa réponse, et ne compte pas lorsque les messages de la personne
  contiennent déjà cette réponse. Le mainteneur du projet l'a demandé après avoir
  lu l'aperçu. Le premier aperçu, jugé sans la règle, est conservé tel quel sous
  `preview-1-judging`, et l'aperçu a été jugé à nouveau avec les instructions
  corrigées. Aucun critère d'aucun scénario n'a changé.
- **Première ébauche abandonnée de la 1.1.** Une première ébauche de la 1.1
  utilisait 36 nouveaux scénarios et un seuil de 9 sur 36. Sa campagne
  (`continuity-1.1.0-20261031-abandoned-36`) a été arrêtée après ses épisodes
  d'ingénierie et un scénario noté, dont les réponses n'ont pas été lues, lorsque
  le mainteneur du projet a rappelé que la 1.1 devait rejouer uniquement la
  partie assistant personnel de la 1.0. Son corpus ne fait pas partie de ce
  dépôt ; aucun de ses épisodes ne compte.
