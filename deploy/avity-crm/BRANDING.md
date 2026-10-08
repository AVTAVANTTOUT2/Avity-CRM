# Identité Avity-CRM

Aucun logo Avity exploitable n'a été retrouvé dans les ressources accessibles.
Le lot utilise un logotype textuel local, sans image tierce :
`packages/twenty-front/public/images/avity/`, avec ses PNG favicon/PWA/social.

Les noms/titres sont dans `src/branding/avity-brand.ts`. `avity-theme.css`, importé
après les thèmes générés, surcharge les accents clair/sombre et boutons d'accent.
Les couleurs d'erreur/statut et l'échelle de l'interface restent gérées par Twenty.
Le titre traduit garde sa traduction et reçoit le suffixe Avity-CRM ; les catalogues
upstream restent intacts. Un logo d'espace personnalisé conserve sa priorité.

Points de liaison dans `packages/twenty-front`, à vérifier lors des upgrades :

| Fichiers | Adaptation |
| --- | --- |
| `index.html`, `public/manifest.json` | Nom, métadonnées, icônes locales |
| `src/index.tsx` | Import CSS après les thèmes |
| `src/utils/title-utils.ts`, `PageTitle.tsx` | Titres |
| `src/modules/auth/components/Logo.tsx` | Logo et nom par défaut |
| `src/pages/auth/SignInUp.tsx` | Nom propre dans une chaîne Lingui existante |
| `DefaultWorkspaceLogo.ts` | Ressource par défaut de navigation/favicon |
| `LeftPanelSkeletonLoader.tsx` | Identité pendant le chargement desktop |
| `PageContentSkeletonLoader.tsx`, `UserOrMetadataLoader.tsx` | Identité pendant le chargement mobile |

Rejouer titres, logo personnalisé, tokens/sélecteurs, clair/sombre, mobile,
chargement et scale. Aucun package, table, cookie, import ou identifiant technique
n'est renommé ; aucun contrôle commercial/licence, rôle, session ou CSRF n'est
modifié. Les licences/attributions Twenty sont conservées.
Les captures de qualification proviennent uniquement du staging synthétique.
