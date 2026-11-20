# Instructions pour les agents — Daemon Harness

Ce fichier s'applique à tout le dépôt. Il fixe les règles opérationnelles que
tout agent doit connaître avant de modifier, tester ou déployer le projet.

## Mission

Daemon Harness est une couche de continuité personnelle pour Hermes et
OpenClaw. Il compose une voix persistante, une mémoire Markdown exacte et un
état présent révisable, puis laisse le modèle et la boucle native de l'hôte
répondre.

La base est saine lorsque la voix comprend la situation, se souvient sans
réciter toute la mémoire, vérifie ce qu'elle ignore, conserve les capacités de
son hôte et entretient sa continuité sans ralentir la conversation.

## Références à lire

Avant toute modification, lire `README.md` et les instructions du périmètre
touché. Avant une évolution de l'architecture, de la mémoire, de la voix, des
outils ou de l'après-tour, lire également :

- `docs/architecture.md` pour les composants, les étapes d'un tour et les
  propriétaires de chaque écriture ;
- `docs/parga.md` et `docs/memory_structure_developer_guide.md` pour la
  mémoire ;
- `docs/productivity-benchmark.md` et
  `benchmarks/productivity_v1/README.md` avant toute évolution du banc public ;
- `docs/continuity-benchmark.md` et `benchmarks/continuity/README.md`
  avant toute évolution du Continuity Benchmark ;
- le fichier `INSTALL.md` de l'intégration concernée avant un déploiement.

Si une demande contredit un invariant documenté, ne pas contourner la
contradiction silencieusement : l'exposer avant d'élargir le changement.

## Invariants non négociables

1. **Une seule voix.** Le daemon compose le contexte ; il n'ajoute ni routeur
   de réponse, ni second agent qui reformule, ni appel LLM supplémentaire avant
   le premier texte sur le chemin nominal.
2. **Les hôtes restent propriétaires de leurs capacités.** Hermes et OpenClaw
   possèdent le canal, la session, le modèle, le streaming, les outils externes
   et leurs autorisations. Ne jamais modifier leur code source : toute
   intégration passe par `integrations/hermes_plugin/`,
   `integrations/hermes_voice_plugin/` ou `integrations/openclaw_plugin/`.
3. **Le daemon ne pilote pas les outils natifs.** Il ne bloque, ne réécrit, ne
   rationne et n'impose ni ordre, ni recette, ni reprise aux outils Web,
   navigateur, calendrier, fichiers ou services de l'hôte. Il peut seulement
   porter une frontière générale de preuve : un résultat vide, bloqué, partiel
   ou ambigu ne confirme pas un fait.
4. **Le code borne, le modèle juge.** Le déterministe protège chemins, tailles,
   dates, provenances, états de file et écritures atomiques. Ne pas ajouter de
   logique métier par mots-clés ou de traitement spécifique à un exemple
   utilisateur pour décider du sens à la place du modèle.
5. **Le Markdown exact fait autorité.** La mémoire personnelle appartient à
   l'utilisateur et reste hors Git. Atlas, embeddings, caches et scènes sont
   des projections reconstructibles ; ils orientent mais ne prouvent pas. Le
   message courant et les sources exactes gardent leur autorité.
   Un agent ne rédige, ne corrige et ne complète jamais directement les
   fichiers de mémoire personnelle pour produire l'état attendu : cela inclut
   l'agenda, les contextes, le focus, les souvenirs et leur index. Une écriture
   factuellement juste reste interdite si elle contourne la provenance et le
   jugement du daemon. Pour réparer un oubli ou une erreur, corriger le
   mécanisme dans un état isolé puis, si nécessaire, rejouer les paroles
   originales de l'utilisateur par le chemin canonique. Une restauration
   octet pour octet depuis une sauvegarde antérieure n'est pas une nouvelle
   rédaction, mais doit rester explicitement signalée.
6. **Les sources externes appartiennent à l'hôte.** Web, mail, calendrier
   professionnel et services distants ne deviennent ni mémoire, ni cache, ni
   outil parallèle du daemon.
7. **Le temps visible reste léger.** La maintenance, la curation et la révision
   de scène travaillent après la réponse ou hors conversation. Une panne
   interne se résorbe, se reprend ou se dégrade proprement ; elle ne bloque pas
   la conversation et ne produit pas de notification technique pour
   l'utilisateur.
8. **Hermes et OpenClaw restent à parité.** Le noyau, la mémoire, les contrats
   de preuve et les outils mémoire sont communs. Une correction locale à un
   hôte ne doit pas modifier la composition commune ni dégrader l'autre hôte.
9. **Aucune donnée sensible dans Git.** Ne jamais versionner mémoire
   personnelle, dialogue réel, secret, jeton, profil d'authentification, trace
   privée ou export brut. Un corpus synthétique utilise toujours une racine et
   une identité de test isolées, jamais la mémoire personnelle.

## Discipline de changement

- **Gel V0 du 26 septembre 2026.** Le cœur validé est gelé. Ne pas rouvrir
  spontanément les prompts, l'architecture, les optimisations, le modèle ou
  les versions hôtes. Seuls les correctifs de défauts reproductibles suivent
  le chemin normal de qualification ci-dessous ; une nouvelle faculté exige
  une décision explicite de périmètre. Documentation et packaging restent
  possibles sans changement de comportement. Ne pas confondre le gel
  technique avec une publication ou un installateur prêt.
- `main` est strictement la branche de production, jamais une branche
  d'expérimentation. Tout candidat dont le comportement, la qualité, la
  latence, les tokens ou l'effet de bord restent à établir doit vivre sur une
  branche et dans un worktree dédiés, avec profils, mémoire et sessions
  isolés. S'il faut une observation sur le runtime réel, la déployer derrière
  une canary explicite et réversible qui laisse `main` inchangée. En l'absence
  de canary disponible, ne pas mener l'expérience en production et ne pas
  promouvoir le candidat.
- Ne fusionner sur `main` qu'un changement dont les critères de sortie ont été
  fixés avant l'essai puis vérifiés sur le chemin réellement affecté. Un test
  synthétique qui ne reproduit pas les mutations d'après-tour, le transport,
  les outils, les tokens et la latence du runtime réel ne peut jamais autoriser
  à lui seul une promotion. La validation sur `main` confirme un déploiement
  déjà qualifié ; elle ne sert pas à découvrir si le candidat fonctionne.
- Inspecter d'abord la branche, le worktree, les pointeurs de plugins, les
  services et les modifications existantes. Préserver tout changement de
  l'utilisateur qui n'appartient pas à la tâche.
- Aucun service personnel, même en canary, ne doit dépendre d'un répertoire
  temporaire (`/tmp`, `/private/tmp`, répertoire temporaire système). Utiliser
  un checkout durable conservé ou une release privée. Les répertoires
  temporaires sont réservés aux tests isolés dont les services sont arrêtés
  avant la fin du test. Le contrôle des pointeurs couvre aussi l'API, le worker
  et le job nocturne macOS ; il refuse par défaut les chemins temporaires.
- Avant de supprimer un worktree, exécuter
  `python3 scripts/check_runtime_pointers.py --before-remove <worktree>`.
  Un chemin source, un lien de plugin ou une configuration active qui pointe
  encore dans ce worktree bloque sa suppression ; remettre d'abord le runtime
  sur une release conservée, redémarrer le service concerné et vérifier sa
  santé.
- `main` est la référence de production. Un résultat n'est pas « sur main »
  tant que le commit local, `origin/main`, les chemins effectivement chargés et
  les services concernés ne sont pas alignés.
- Placer une règle commune dans `src/daemon_core/` et seulement la colle
  propre à l'hôte dans son intégration. Ne pas dupliquer le cœur dans les deux
  plugins.
- Préférer le plus petit changement général qui rétablit l'invariant. Ne pas
  réparer un symptôme par une procédure spécifique à une tâche, un site ou un
  outil particulier.
- Tester dans un état jetable. Une évaluation ne doit toucher ni une mémoire
  personnelle, ni un canal réel, ni une action externe, sauf autorisation
  explicite et protocole borné.
- Exécuter les tests ciblés, puis la suite commune proportionnée au risque.
  Vérifier aussi les migrations, le déploiement et l'autre hôte lorsque le
  contrat partagé peut être affecté.

## Expériences et latence

Toute expérience qui peut modifier une décision active doit comparer la
solution au comportement natif ou à la référence canonique, avec le même
modèle, les mêmes outils et un état isolé. Alterner l'ordre des bras lorsque la
variance peut influer.

Mesurer au minimum :

- l'exactitude et la qualité de la réponse ou de l'effet produit ;
- le temps complet visible par le banc ;
- le temps interne de l'hôte et le calcul propre du daemon lorsque disponibles ;
- le nombre et la nature des étapes d'outils ;
- le premier fragment visible et le transport réel lorsqu'ils sont exposés ;
- les limites, incidents de banc et résultats atypiques.

Ne jamais conclure à partir du seul texte final ou d'une moyenne qui masque la
variance. Conserver les résultats négatifs et les hypothèses rejetées.

Un banc destiné à soutenir une affirmation publique doit être spécifié avant
ses résultats et rester auditable dans les deux sens : recalcul hors ligne à
partir des artefacts anonymisés, puis rejeu complet avec des profils jetables.
Versionner les fixtures synthétiques, vérificateurs déterministes, manifestes,
règles d'exclusion et générateur de rapport. Qualifier chaque compteur de
tokens comme rapporté par le fournisseur, rapporté par l'hôte, estimé par un
tokenizer versionné ou indisponible ; une valeur absente n'est jamais zéro.
Une mauvaise réponse, une lenteur ou une surconsommation après réception du
tour est un résultat, pas un motif d'exclusion.

Fixer la langue de chaque corpus avant exécution. Le banc public V1 est en
anglais ; une localisation française éventuelle forme une piste versionnée et
notée séparément, jamais agrégée aux chiffres principaux anglais.

Une expérience décisive est incomplète tant que son rapport n'est pas écrit.
Un rapport qui soutient une affirmation publique est ajouté à
`docs/evaluations/` et versionné sur `main` ; les autres rapports sont
conservés par le mainteneur hors de ce dépôt. Les traces sensibles ou lourdes restent hors Git ; le rapport conserve
le protocole, les révisions, les mesures, la décision et les réserves nécessaires
pour ne pas refaire le même travail.

## Déploiement et bilan

Après un changement de runtime ou de plugin :

1. pousser le commit validé sur `origin/main` ;
2. vérifier le chemin réellement chargé par chaque hôte concerné ;
3. déployer le workspace ou la configuration gérée si nécessaire ;
4. redémarrer uniquement les services affectés, après le dernier commit qui les
   concerne ;
5. vérifier processus, version, connectivité et état dégradé éventuel ;
6. confirmer que l'autre hôte et la composition commune sont restés intacts.

Un bilan doit indiquer clairement : branche et commit, fichiers modifiés,
emplacement déployé, services redémarrés ou non, tests exécutés, mesures de
latence, limites restantes et une seule prochaine étape recommandée.

Toute modification d'un installateur, d'un workspace géré ou d'une
configuration hôte qui change la mémoire native, le Dreaming, les skills, les
outils, l'affichage ou le rollback doit mettre à jour dans le même commit la
référence `docs/installation_impact.md`, le résumé « Exact installation
impact » du `README.md` s'il devient inexact, et le `INSTALL.md` de
l'intégration concernée. Ne jamais annoncer qu'une capacité hôte est désactivée
si le script ne l'applique pas ou si le contrôle de déploiement ne le vérifie
pas réellement.

## Frontière avec le workspace OpenClaw

`integrations/openclaw_plugin/workspace/AGENTS.md` est un artefact de runtime
injecté dans le contexte conversationnel OpenClaw. Il doit rester court et ne
contenir que les règles nécessaires à la voix et aux outils pendant un échange.
Ne pas y recopier les règles de contribution, de Git, de test, d'évaluation ou
de déploiement de ce fichier racine.
