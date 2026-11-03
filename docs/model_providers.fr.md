# Fournisseurs de modèles

Un modèle n'est proposé pour un daemon que si tout le daemon fonctionne bien
avec lui : ses réponses, ce qu'il retient, et ce qu'il suit d'un message à
l'autre (ce qui se passe en ce moment, ce qui reste en suspens, ce qui est
réglé). Un modèle qui n'en réussit qu'une partie n'est pas proposé, avec ou sans
avertissement.

| Fournisseur | Modèle testé | Testé le | Proposé | Pourquoi |
| --- | --- | --- | --- | --- |
| Abonnement ChatGPT | GPT-6 Astra | 2026-10-03, 2026-10-24 | Oui | Le daemon de référence fonctionne entièrement avec lui depuis le 2026-10-03, chaque jour. Un nouveau daemon lui fournit les mêmes éléments que la référence. |
| Abonnement ChatGPT | GPT-6.1 Sol | 2026-10-29 | Proposé en attendant le test complet | A suivi la situation aussi bien que la référence dans quatre conversations de test. Ses réponses et sa mémoire sur un vrai daemon ne sont pas encore testées. |
| Abonnement ChatGPT | GPT-6.1 Terra | 2026-10-29 | Pas encore | A suivi la situation aussi bien que la référence dans quatre conversations de test, trois fois plus vite. Ses réponses et sa mémoire sur un vrai daemon ne sont pas encore testées. |
| Abonnement ChatGPT | GPT-5.6 Terra | 2026-08-31 | Non | Retiré le 2026-10-29 : Companion avertissait que l'expérience pouvait être dégradée avec lui, et un modèle qui demande un avertissement n'est pas proposé. |
| Mistral (clé API) | Mistral Large 4 | 2026-10-29, de nouveau le soir même après sa sortie publique | Non | Perd le fil de ce qui se passe dans la conversation, et enregistre comme faits des choses non confirmées. Voir ci-dessous. |
| OpenAI (clé API) | — | Non testé | À décider | Même famille de modèles que l'abonnement, mais par une autre voie, avec d'autres réglages. Pas encore testé. |
| Anthropic (clé API) | Claude Opus 5.5 | En test | Seulement ce modèle | En test sur un vrai daemon depuis le 2026-10-29. Les autres modèles Claude ne sont pas proposés. |
| SpaceXSI (clé API) | Grok 4.7 | 2026-10-29 | Non | Ses réponses sont plus courtes, plus sèches et plus lentes que celles de la référence. Voir ci-dessous. |

## Pourquoi Mistral Large 4 n'est pas proposé

Après chaque message, le daemon met à jour sa vision de la conversation : ce
qui se passe, ce qui reste en suspens, ce qui est réglé. Avec Mistral Large 4,
cette vision n'est jamais mise à jour, pour deux raisons :

- **Trop lent.** Chaque mise à jour prend de 1 à 7 minutes, avec la réflexion
  maximale de Mistral, qu'il utilise par défaut. Le daemon arrête d'attendre
  au bout de 2 minutes. Sans réflexion, il répond en moins de 30 secondes,
  mais se trompe davantage.
- **Des faits qu'il ne peut pas justifier.** Même avec tout le temps
  nécessaire, il enregistre comme certaines des choses que personne n'a
  confirmées, comme les propres suggestions de l'assistant, ou cite des
  sources incohérentes. Le daemon rejette alors toute la mise à jour, et c'est
  voulu : il ne doit jamais transformer une supposition en fait.

Sur le daemon de test du propriétaire, il a échoué avec les deux réglages, et
sans réflexion il a aussi échoué sur deux des quatre conversations de test.
Ses réponses et ce qu'il retient fonctionnaient, mais un daemon qui perd le fil
de la situation n'offre pas l'expérience complète. Il réessaie aussi en
arrière-plan après chaque message, ce qui peut consommer du crédit sur le
compte Mistral.

## Pourquoi Grok 4.7 (SpaceXSI) n'est pas proposé

Mesuré le 2026-10-29, avec le même daemon, la même persona et la même
conversation de sept messages envoyée à Grok 4.7 et à GPT-6 Astra :

- **Des réponses plus courtes.** Après les trois réponses d'accueil, Grok a
  écrit moitié moins qu'Astra avec son réglage de réflexion habituel (809
  caractères contre 1 706), et trois fois moins avec la réflexion maximale
  (524). Ses réponses sont des phrases sèches (« Oh. Ta sœur, enceinte. C'est
  pour quand ? ») là où Astra réagit à la personne.
- **Plus lent.** 19 secondes par réponse en moyenne, 29 avec la réflexion
  maximale, contre 8 pour Astra.
- **Sur un vrai daemon de test**, le propriétaire a trouvé ses réponses sobres
  et impersonnelles.

Sur ce daemon de test, la persona et le contexte du daemon lui parvenaient
en entier (vérifié par la taille des demandes, environ 12 000 jetons par
message).

Avant de proposer un nouveau fournisseur (par exemple OpenRouter),
vérifier les réglages de réflexion qu'il accepte, le temps qu'il met pour le
travail du daemon entre les messages, le format de ses réponses, et passer les
mêmes tests.

Rapports : réponses de Grok 4.7 et d'Astra,
GPT-6.1 Sol et Terra,
Mistral Large 4,
GPT-5.6 Terra,
réponses de GPT-6 Sol et Terra,
parité avec la référence.
