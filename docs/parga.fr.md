# PARGA : Le Classement De La Mémoire

[English](parga.md) · **Français**

La mémoire d'un daemon est un dossier de notes Markdown (un format de texte
simple) sur votre ordinateur. Chaque note est un **souvenir** : elle garde ce
que le daemon sait d'un sujet durable de votre vie, comme une personne, un
projet, une préférence, une situation ou une idée, avec son histoire et
l'origine de chaque fait.

**PARGA** est la façon dont ces souvenirs sont rangés. Le nom reprend
l'initiale des cinq dossiers : **Projects**, **Areas**, **Resources**,
**Garden** et **Archives**. Ce classement s'inspire notamment de PARA
(Tiago Forte), d'IPCRA (Eliott Meunier) et du Zettelkasten, adaptés à un
assistant qui tient votre mémoire à jour après chaque échange.

## Pourquoi un classement

Un historique de conversation permet de retrouver des paroles, mais oblige à
reconstituer leur histoire à chaque fois. À l'inverse, une liste de petits
faits isolés perd ce qui les relie : l'objectif, les tentatives, les personnes
concernées, la raison d'un blocage, le dernier état confirmé.

Un souvenir rassemble donc toute la continuité d'un sujet. La note d'une
réparation garde la panne, les essais qui ont échoué, la pièce attendue et la
dernière nouvelle de l'atelier. Quand vous en reparlez, le daemon comprend
votre message à la lumière de cette histoire, sans avoir à relire toute sa
mémoire.

Le classement en dossiers répond à une autre question : où chercher, et
comment vous, vous retrouvez-vous dans votre propre mémoire. Les dossiers sont
de vrais dossiers, que vous pouvez parcourir, ouvrir et réorganiser, par
exemple dans [Obsidian](https://obsidian.md), une application de prise de notes
qui ouvre directement ce dossier.

## Les cinq dossiers

| Dossier | Ce qu'on y trouve | Exemple | À ne pas confondre |
| --- | --- | --- | --- |
| **Projects** | Un résultat que vous poursuivez et qui aura une fin : son périmètre, ses décisions, ce dont il dépend et son avancement. | Préparer un anniversaire ; déployer un outil dans une équipe. | Une suggestion du daemon n'est pas un engagement : elle ne crée pas de projet. |
| **Areas** | Ce qui dure sans date de fin : responsabilités et rôles, personnes et relations, préférences, situations que vous traversez. | Une collègue ; la santé ; une réparation en attente. | Une routine n'est pas un projet. |
| **Resources** | Une référence précise à utiliser plus tard, comme un livre, un podcast ou un guide, avec la raison pour laquelle vous la gardez. | Un podcast à écouter avant une négociation. | Sa présence ne prouve pas qu'il a été écouté ou lu. |
| **Garden** | Une idée, un principe ou un enseignement que vous avez formulé ou adopté, et qui pourrait servir dans une tout autre partie de votre vie. La note grandit chaque fois que vous revenez à l'idée. | « Essayer un changement sur un petit pilote réversible avant de le généraliser. » | Un fait de votre vie va dans le dossier du sujet concerné ; une référence (livre, article, vidéo) va dans `Resources` ; une idée suggérée par le daemon et que vous n'avez pas reprise n'est pas gardée. |
| **Archives** | Ce qui est terminé ou devenu inactif, avec son état final exact, toujours consultable. | Un voyage passé, avec ce qui a été décidé et vécu ; le récit d'un épisode passé. | Archivé ne veut pas dire réussi : une annulation, une pause et une réussite restent des états différents. |

Chaque dossier peut contenir des sous-dossiers, qui évoluent avec vos besoins
(`Areas/Work`, `Areas/Family`, `Projects/Home`…). Les noms de dossiers sont en
anglais ; le contenu des notes reste dans la langue de vos conversations.

## Garden : le jardin de vos idées

**Les autres dossiers rangent ce qui vous arrive ; Garden fait pousser ce que
vous pensez.**

`Garden` range vos idées : un principe, une conviction, une méthode ou une
leçon tirée de l'expérience, que vous avez formulés ou adoptés. Comme dans un
jardin, ce qu'on y plante pousse : la note s'enrichit chaque fois que vous
revenez à l'idée ou l'appliquez ailleurs, et son historique garde vos nuances
et vos changements d'avis.

Une idée n'appartient pas à un seul sujet. Vous rénovez votre cuisine et vous
apprenez qu'il vaut mieux tout mesurer deux fois avant de commander. Le
chantier va dans `Projects`, puis dans `Archives` une fois terminé. La leçon,
elle, va dans `Garden` : elle reste disponible pour un déménagement ou un autre
achat, longtemps après la fin du chantier. Rangée dans le projet, elle aurait
été archivée avec lui.

**La question à se poser.** Cette note pourrait-elle servir dans une tout
autre partie de votre vie ? Si oui, elle va dans `Garden`. Si elle décrit une
situation précise, elle va dans le dossier de cette situation.

Ce qui n'y va pas :

- **un fait de votre vie**, même important : il appartient au projet ou au
  domaine concerné. « Ma sœur compte énormément pour moi » décrit une relation
  et va dans `Areas` ; « une décision se prend mieux après une nuit de
  sommeil » est une idée qui peut servir partout et va dans `Garden` ;
- **une référence** (livre, article, vidéo) : elle va dans `Resources`. Une
  idée qui en vient et que vous adoptez peut avoir sa propre note Garden,
  reliée à cette référence ;
- **une idée du daemon** que vous n'avez pas reprise.

Si l'idée devient elle-même un engagement, par exemple vous décidez de lancer
le pilote qu'elle décrivait, la note passe dans `Projects`. Le daemon ne
retravaille pas une note Garden de lui-même entre deux conversations, et il
n'invente pas d'idées pour remplir le dossier.

## Un sujet, une note, des liens

Chaque souvenir a **un seul emplacement**. Une personne impliquée dans deux
projets garde une seule note, dans `Areas`, et chaque projet la cite par un
lien. Les liens s'écrivent comme dans Obsidian, `[[Areas/Work/Nora]]`, suivis
d'une phrase qui dit pourquoi les deux notes sont liées. **MemoryAgent**, le
jugement d'après-réponse qui entretient les souvenirs, ne relie que des notes qui existent et n'ajoute pas de liens pour remplir un graphe.

Le nom d'un dossier ne prouve rien. Appartenir au même foyer ne veut pas dire
être présent aujourd'hui ; les goûts d'une personne restent attachés à cette
personne. Toute relation entre deux sujets est écrite dans une note et appuyée
sur ce que vous avez dit.

## L'anatomie d'un souvenir

Un souvenir commence par quelques propriétés, qui servent au daemon à le
retrouver, puis un texte organisé en sections. Voici une note de projet et la
note Garden à laquelle elle renvoie. Les noms de sections sont en anglais, le
texte dans la langue de la conversation.

````markdown
---
memory_id: theme/work/orion
kind: project
memory_areas:
  - work
subjects:
  - orion
facets:
  - rollout
aliases:
  - Pilote Orion
---
# Déploiement d'Orion

## Scope
Déployer Orion auprès de l'équipe support, en commençant par un pilote.

## Last confirmed state
Le 2026-01-12, le pilote est validé ; sa date de lancement reste à confirmer.

## History
- 2026-01-08 : un lancement global est envisagé.
- 2026-01-12 : l'équipe choisit un pilote pour limiter le risque de perturbation.

## Still unknown
- Date de lancement confirmée.

## Links
- [[Areas/Work/Nora]] : chargée du suivi du pilote.
- [[Garden/Work/Petits pilotes réversibles]] : le principe qui a fait choisir un pilote.
````

````markdown
---
memory_id: theme/work/small_reversible_pilots
kind: knowledge
memory_areas:
  - work
subjects:
  - pilots
facets:
  - risk
---
# Petits pilotes réversibles

## Scope
Un principe de travail adopté par l'utilisateur : essayer un changement sur un
petit périmètre réversible avant de le généraliser.

## History
- 2026-01-12 : adopté en choisissant le pilote d'Orion : « Désormais je
  commencerai toujours par un petit pilote qu'on peut annuler. »

## Still unknown
- S'il l'applique aussi en dehors du travail.

## Links
- [[Projects/Work/Déploiement d'Orion]] : là où le principe a été adopté.
````

| Propriété | Rôle |
| --- | --- |
| `memory_id` | L'identifiant du souvenir, fixé à sa création. Il ne change pas quand le fichier est renommé ou déplacé. |
| `kind` | La nature du sujet : projet, domaine, situation, personne, groupe, organisation, lieu, objet, ressource, connaissance ou épisode. |
| `memory_areas` | Les domaines de vie concernés, par exemple `work` ou `health`. |
| `subjects`, `facets` | Des étiquettes qui aident l'index à retrouver le souvenir. Elles ne prouvent aucune relation. |
| `aliases` | D'autres noms du même sujet. |

Les sections disent ce qui est connu et depuis quand : le **périmètre**
(Scope), le **dernier état confirmé** et sa date, l'**historique** des
étapes, ce qui **reste inconnu** et les **liens**. Une section n'apparaît que
si elle a un contenu. L'entretien complète l'historique au lieu de le
remplacer par un résumé : on doit toujours pouvoir comprendre comment on en
est arrivé là.

Les dates s'écrivent `AAAA-MM-JJ`, ou `AAAA-MM` et `AAAA` quand on n'en sait
pas plus. La date d'un événement, celle où il a été mentionné et celle où le
fichier a été modifié sont trois choses différentes.

Ces propriétés et ces liens sont ceux d'Obsidian : la mémoire s'y lit et s'y
modifie sans extension.

## Qui range, et comment un souvenir change de dossier

Après chaque échange, MemoryAgent décide s'il faut créer une note, en
compléter une ou la corriger. Quand il crée une note, il choisit son dossier.
Il ne crée pas une note par échange : beaucoup de messages n'apportent rien
de durable.

Un souvenir peut changer de dossier, mais seulement après un élément nouveau
de votre part qui le justifie. MemoryAgent cite alors vos mots exacts et
explique la raison du changement. Deux cas typiques :

- un projet que vous dites terminé ou abandonné passe dans `Archives` ;
- une idée de `Garden` devenue un engagement concret passe dans `Projects`.

Lors d'un déplacement, le `memory_id` ne change pas, et les liens que d'autres
notes font vers la note déplacée sont mis à jour vers son nouvel emplacement.
Rien n'est archivé parce qu'on n'en parle plus : le silence ne résout pas un
problème ouvert, et un souvenir archivé reste lisible et retrouvable.

Vous pouvez aussi ranger, renommer ou modifier vos notes vous-même. Le daemon
respecte l'emplacement choisi et retrouve la note par son `memory_id`.

## Pourquoi pas de dossier « à trier »

Beaucoup de méthodes de prise de notes commencent par une boîte de réception,
un dossier où l'on dépose tout avant de trier. PARGA n'en a pas, car rien ne
serait perdu sans elle :

- **L0**, le niveau brut de l'historique de conversation (*Level 0*, aussi
  appelé *Ground Zero*), garde chaque message mot pour mot ;
- les traitements d'entretien en attente restent dans une file jusqu'à leur
  réalisation, même après un redémarrage.

Une information ambiguë reste donc une question ouverte, et une panne reste
un travail à reprendre ; ni l'une ni l'autre ne devient un fait inventé pour
être rangé.

## Pour aller plus loin

- [Le daemon en un coup d'œil](overview.fr.md) : comment la mémoire est lue
  avant chaque réponse et entretenue après.
- [Référence de la mémoire](memory_structure_developer_guide.md) : formats,
  provenance, protections d'écriture et code concerné.
- [Votre mémoire](memory.md) (en anglais) : ouvrir et gérer la mémoire dans
  Obsidian.
