// Every word the site shows, in English and French. English is the source; French must have the same shape (the Dict
// type enforces it), so a sentence added in one language and forgotten in the other fails the build.
// Writing rules for both: plain words, no money, no accuracy numbers, services not built yet say so.

export type Locale = "en" | "fr";
export const LOCALES: Locale[] = ["en", "fr"];
export const LOCALE_PATH: Record<Locale, string> = { en: "/", fr: "/fr" };
export const LOCALE_COOKIE = "noza_lang";

type Step = [string, string, string];
type CardText = { tag: string; title: string; text: string };

const en = {
  meta: {
    title: "Noza Labs · Reading the land and the lines that cross it",
    description: "Drone and satellite intelligence for Africa's farms, towers and power lines. Every answer says how sure it is.",
    keywords: ["drone", "agriculture", "Africa", "power line inspection", "tower inspection", "crop map", "Ingabe"],
  },
  nav: {
    links: { read: "What we read", where: "Where we fly", accuracy: "Accuracy", how: "How it works", access: "Access" },
    home: "Noza Labs, back to top",
    signIn: "Sign in",
    openIngabe: "Open Ingabe",
    requestAccess: "Request access",
    openMenu: "Open menu",
    closeMenu: "Close menu",
    switchTo: "Lire en français",
  },
  hero: {
    kicker: "Noza Labs · Kigali",
    title: "We read the land, and the *lines* that cross it.",
    lead: "Drone and satellite intelligence for Africa's farms, towers and power lines. Every answer says how sure it is.",
    talk: "Talk to us",
    motto: ["See it from above.", "Answer it on the ground."],
    picture: "A drone reads a hillside of farm plots and a power line",
  },
  facts: [
    { k: "Drone photos", v: "3 cm a pixel" },
    { k: "Satellite", v: "Every 5 days" },
    { k: "Rainfall", v: "Daily, since 1981" },
    { k: "Crop names", v: "Two looks must agree" },
  ],
  read: {
    kicker: "What we read",
    title: "What one flight can tell you.",
    lead: "We study where answers go wrong in the field, then build the checks that keep them right.",
    farms: { label: "Farms", note: "Live in Ingabe" },
    lines: { label: "Towers & power lines", note: "In development · pilot flights open" },
    live: "Live",
    building: "Building",
    cta: "Tell us what you need to see",
    cards: {
      crops: { tag: "Crop map", title: "Which crop is in each plot", text: "Two separate looks at every plot. When they agree we name the crop; when they don't, we say not sure and show both guesses." },
      count: { tag: "Plant count", title: "How many plants stand", text: "Plants counted plot by plot, given as a range we can stand behind, not a single proud number." },
      gaps: { tag: "Gaps", title: "Where the crop is missing", text: "Open soil inside a plot is measured, and flagged only when it is a real share of the plot." },
      tower: { tag: "Towers", title: "Tower and insulator checks", text: "Each tower seen from the same angles every flight, so a missing part or new damage stands out." },
      vegetation: { tag: "Vegetation", title: "Trees growing into the line", text: "Tree height against the wire, span by span, so crews cut where it matters first." },
      corridor: { tag: "Right of way", title: "What is inside the corridor", text: "New houses, fields and roads inside the line's corridor, found before they become a danger." },
    } as Record<"crops" | "count" | "gaps" | "tower" | "vegetation" | "corridor", CardText>,
    pause: "Pause drawings",
    play: "Play drawings",
  },
  where: {
    kicker: "Where we fly",
    title: "Hillsides, marshlands, and the lines between them.",
    lead: "Seven kinds of place, one way of reading them. Hover a world to see it the way the drone does.",
    worlds: {
      hills: ["Hillside farms", "Terraces, mixed plots, bananas"],
      marsh: ["Marshland rice", "Paddies and channels"],
      tea: ["Tea estates", "Rows on steep slopes"],
      scheme: ["Irrigation schemes", "Blocks, canals, seasons"],
      corridor: ["Transmission lines", "Towers, spans, right of way"],
      telecom: ["Telecom & monitoring towers", "Masts, antennas, sites"],
      distribution: ["Village power lines", "Poles and wires along roads"],
    } as Record<string, [string, string]>,
  },
  accuracy: {
    kicker: "Accuracy first",
    titleStart: "“Not sure” is",
    titleItalic: "an answer.",
    p1: "A confident wrong label is worse than no label. Farmers, agronomists and insurers act on what we show, so a crop is named only when two independent looks at the plot agree.",
    p2: "Every field check is kept and counted. How often we are right is measured against them, and each answer carries its level: low until enough fields have been checked, higher only when the record earns it.",
    tiles: [
      ["Two looks", "agree, or we abstain"],
      ["Field checks", "keep the score"],
      ["Every answer", "says how sure"],
    ] as [string, string][],
  },
  twoLooks: {
    plot: "Plot",
    look: "Look",
    whatWeShow: "What we show",
    agree: "both looks agree",
    notSure: "Not sure",
    or: "or",
    q: "?",
    namedNote: "Named on the map, with how sure we are.",
    checkNote: "Marked for a quick check in the field. Your answer teaches the next look.",
    crops: { maize: "Maize", beans: "Beans", cassava: "Cassava" } as Record<string, string>,
  },
  how: {
    kicker: "How it works",
    titleStart: "Fly. Read.",
    titleItalic: "Ask.",
    steps: [
      ["01", "Fly", "Upload a drone flight, or we fly it with you. Satellite, rain and soil data for the same place are already waiting."],
      ["02", "Read", "Plots outlined, crops named, plants counted, gaps found. Each answer is drawn on the photo, with how sure we are."],
      ["03", "Ask", "Ask Sage in plain words. It knows when the drone photo answers your question and when the satellite does, and says which it used."],
    ] as Step[],
  },
  sage: {
    example: "Example answer",
    question: "Which plots should I visit first this week?",
    thinking: "reading the photo and the rain…",
    lead: "Start with the two plots where the crop is thin.",
    // French puts a non-breaking space before a colon.
    colon: ":",
    points: [
      { k: "Drone photo", v: "open soil covers a real share of both plots, mostly along the lower edge." },
      { k: "Satellite rain", v: "the last ten days were drier than usual for this point in the season." },
      { k: "Not sure yet", v: "one plot could be maize or beans. A quick look on the ground settles it." },
    ],
    sources: ["Drone photo", "CHIRPS rainfall", "Field checks"],
  },
  access: {
    kicker: "Access to Ingabe",
    titleStart: "By invitation,",
    titleItalic: "on purpose.",
    lead: "Ingabe holds field records, drone photographs and inspection findings that belong to the companies we work with. So nobody opens an account on their own: every person comes in through an invitation, from their company's admin or from our team.",
    steps: [
      ["01", "Ask", "Request access with your name, your company and what you need to see. It takes a minute."],
      ["02", "We review", "Our team reads every request and answers by email. If your company already works with us, its admin can add you directly."],
      ["03", "Invitation", "Once approved, an invitation arrives by email. It opens Ingabe inside your company, with your company's maps and work."],
      ["04", "Sign in", "Type your email and Ingabe sends a one-time code. There is no password to remember, share or lose."],
    ] as Step[],
    request: "Request access",
    already: "Already invited? Sign in",
    boxTitle: "How sign-in protects your work",
    points: [
      ["No passwords.", "Each sign-in uses a one-time code sent to your email, so there is nothing to steal from a reused or written-down password."],
      ["A dedicated sign-in service.", "Sign-in runs on WorkOS, an identity service built for business software. Ingabe never handles your codes."],
      ["Each company sees only its own work.", "Maps, photographs and findings are kept apart in the database itself, not only on screen."],
      ["Access can end at once.", "Your company's admin decides who joins and can remove anyone; a removed person is signed out at their next click."],
    ] as [string, string][],
    waiting: "Waiting for a code that never arrives? Your address may not be invited yet. Sign-in only sends codes to invited people, so request access, or call us on",
  },
  contact: {
    kicker: "Noza Labs",
    titleStart: "Tell us what you need to see",
    titleItalic: "from the air.",
    lead: "Farms, cooperatives, insurers, grid and tower operators: bring a place and a question. We will fly it and answer it with you.",
    call: "Call",
  },
  footer: {
    mark: "One mark on everything we make, Ingabe included: the n of noza, and a full stop.",
    readTitle: "What we read",
    readLinks: ["Crop map", "Plant counts", "Towers & lines"],
    appTitle: "Ingabe app",
    appLinks: ["Open the app", "Sign in", "Request access", "How it works", "How access works"],
    contact: "Contact",
    place: "Kigali, Rwanda",
    made: "Made in Kigali",
  },
  // Labels the drone drawing writes on the picture (upper-cased when drawn).
  drawing: {
    maize: "maize",
    cassava: "cassava",
    beans: "beans",
    "not sure · beans or maize?": "not sure · beans or maize?",
    "not sure · maize or beans?": "not sure · maize or beans?",
    "tower 2 · insulators": "tower 2 · insulators",
    "tree close to line": "tree close to line",
    "mast · antennas": "mast · antennas",
  } as Record<string, string>,
};

export type Dict = typeof en;

const fr: Dict = {
  meta: {
    title: "Noza Labs · Lire la terre et les lignes qui la traversent",
    description:
      "Drones et satellites au service des fermes, des pylônes et des lignes électriques d'Afrique. Chaque réponse indique son degré de certitude.",
    keywords: ["drone", "agriculture", "Afrique", "inspection de lignes électriques", "inspection de pylônes", "carte des cultures", "Ingabe"],
  },
  nav: {
    links: { read: "Ce que nous lisons", where: "Où nous volons", accuracy: "Fiabilité", how: "Comment ça marche", access: "Accès" },
    home: "Noza Labs, retour en haut",
    signIn: "Se connecter",
    openIngabe: "Ouvrir Ingabe",
    requestAccess: "Demander l'accès",
    openMenu: "Ouvrir le menu",
    closeMenu: "Fermer le menu",
    switchTo: "Read in English",
  },
  hero: {
    kicker: "Noza Labs · Kigali",
    title: "Nous lisons la terre, et les *lignes* qui la traversent.",
    lead: "Drones et satellites au service des fermes, des pylônes et des lignes électriques d'Afrique. Chaque réponse indique son degré de certitude.",
    talk: "Contactez-nous",
    motto: ["Le voir d'en haut.", "Y répondre sur le terrain."],
    picture: "Un drone lit une colline de parcelles agricoles et une ligne électrique",
  },
  facts: [
    { k: "Photos de drone", v: "3 cm par pixel" },
    { k: "Satellite", v: "Tous les 5 jours" },
    { k: "Pluie", v: "Chaque jour, depuis 1981" },
    { k: "Noms des cultures", v: "Deux lectures doivent concorder" },
  ],
  read: {
    kicker: "Ce que nous lisons",
    title: "Ce qu'un seul vol peut vous apprendre.",
    lead: "Nous étudions où les réponses se trompent sur le terrain, puis nous construisons les contrôles qui les gardent justes.",
    farms: { label: "Exploitations agricoles", note: "Disponible dans Ingabe" },
    lines: { label: "Pylônes et lignes électriques", note: "En développement · vols pilotes ouverts" },
    live: "Disponible",
    building: "En construction",
    cta: "Dites-nous ce que vous avez besoin de voir",
    cards: {
      crops: {
        tag: "Carte des cultures",
        title: "Quelle culture dans chaque parcelle",
        text: "Deux lectures indépendantes de chaque parcelle. Quand elles concordent, nous nommons la culture ; sinon, nous disons « pas sûr » et montrons les deux hypothèses.",
      },
      count: {
        tag: "Comptage des plants",
        title: "Combien de plants ont levé",
        text: "Les plants sont comptés parcelle par parcelle et donnés sous forme de fourchette que nous pouvons défendre, pas d'un seul chiffre trop sûr de lui.",
      },
      gaps: {
        tag: "Manques",
        title: "Là où la culture manque",
        text: "La terre nue à l'intérieur d'une parcelle est mesurée, et signalée seulement quand elle en couvre une part réelle.",
      },
      tower: {
        tag: "Pylônes",
        title: "Contrôle des pylônes et des isolateurs",
        text: "Chaque pylône est vu sous les mêmes angles à chaque vol : une pièce manquante ou un nouveau dégât saute aux yeux.",
      },
      vegetation: {
        tag: "Végétation",
        title: "Les arbres qui s'approchent de la ligne",
        text: "La hauteur des arbres face au câble, portée par portée, pour que les équipes élaguent d'abord là où c'est urgent.",
      },
      corridor: {
        tag: "Emprise",
        title: "Ce qui se trouve dans le couloir",
        text: "Maisons, champs et routes apparus dans le couloir de la ligne, repérés avant de devenir un danger.",
      },
    },
    pause: "Mettre les dessins en pause",
    play: "Relancer les dessins",
  },
  where: {
    kicker: "Où nous volons",
    title: "Collines, marais, et les lignes qui les relient.",
    lead: "Sept types de lieux, une seule façon de les lire. Survolez un monde pour le voir comme le voit le drone.",
    worlds: {
      hills: ["Fermes de colline", "Terrasses, parcelles mixtes, bananiers"],
      marsh: ["Riz de marais", "Rizières et canaux"],
      tea: ["Plantations de thé", "Rangs sur pentes raides"],
      scheme: ["Périmètres irrigués", "Blocs, canaux, saisons"],
      corridor: ["Lignes de transport", "Pylônes, portées, emprise"],
      telecom: ["Tours télécom et de surveillance", "Mâts, antennes, sites"],
      distribution: ["Lignes électriques des villages", "Poteaux et câbles le long des routes"],
    },
  },
  accuracy: {
    kicker: "La fiabilité d'abord",
    titleStart: "« Pas sûr » est",
    titleItalic: "une réponse.",
    p1: "Une étiquette fausse donnée avec assurance est pire que pas d'étiquette du tout. Agriculteurs, agronomes et assureurs agissent sur ce que nous montrons : une culture n'est donc nommée que lorsque deux lectures indépendantes de la parcelle concordent.",
    p2: "Chaque vérification sur le terrain est conservée et comptée. Notre taux de réussite est mesuré par rapport à elles, et chaque réponse porte son niveau : faible tant que trop peu de champs ont été vérifiés, plus élevé seulement quand les résultats le justifient.",
    tiles: [
      ["Deux lectures", "concordent, ou nous nous abstenons"],
      ["Les vérifications", "sur le terrain tiennent le score"],
      ["Chaque réponse", "dit son degré de certitude"],
    ],
  },
  twoLooks: {
    plot: "Parcelle",
    look: "Lecture",
    whatWeShow: "Ce que nous affichons",
    agree: "les deux lectures concordent",
    notSure: "Pas sûr",
    or: "ou",
    q: " ?",
    namedNote: "Nommée sur la carte, avec notre degré de certitude.",
    checkNote: "Signalée pour une vérification rapide sur le terrain. Votre réponse instruit la prochaine lecture.",
    crops: { maize: "Maïs", beans: "Haricots", cassava: "Manioc" },
  },
  how: {
    kicker: "Comment ça marche",
    titleStart: "Survoler. Lire.",
    titleItalic: "Demander.",
    steps: [
      ["01", "Survoler", "Téléversez un vol de drone, ou nous le faisons avec vous. Les données satellite, de pluie et de sol du même endroit vous attendent déjà."],
      ["02", "Lire", "Parcelles délimitées, cultures nommées, plants comptés, manques repérés. Chaque réponse est dessinée sur la photo, avec notre degré de certitude."],
      ["03", "Demander", "Posez vos questions à Sage avec des mots simples. Il sait quand la photo de drone répond à votre question et quand c'est le satellite, et il dit lequel il a utilisé."],
    ],
  },
  sage: {
    example: "Exemple de réponse",
    question: "Quelles parcelles dois-je visiter en premier cette semaine ?",
    thinking: "lecture de la photo et de la pluie…",
    lead: "Commencez par les deux parcelles où la culture est clairsemée.",
    colon: "\u00a0:",
    points: [
      { k: "Photo de drone", v: "la terre nue couvre une part réelle des deux parcelles, surtout le long du bord inférieur." },
      { k: "Pluie par satellite", v: "les dix derniers jours ont été plus secs que d'habitude à ce moment de la saison." },
      { k: "Pas encore sûr", v: "une parcelle pourrait être du maïs ou des haricots. Un rapide coup d'œil sur place tranchera." },
    ],
    sources: ["Photo de drone", "Pluie CHIRPS", "Vérifications terrain"],
  },
  access: {
    kicker: "Accès à Ingabe",
    titleStart: "Sur invitation,",
    titleItalic: "et c'est voulu.",
    lead: "Ingabe contient des relevés de terrain, des photos de drone et des résultats d'inspection qui appartiennent aux entreprises avec lesquelles nous travaillons. Personne n'ouvre donc de compte seul : chaque personne entre par une invitation, envoyée par l'administrateur de son entreprise ou par notre équipe.",
    steps: [
      ["01", "Demander", "Demandez l'accès avec votre nom, votre entreprise et ce que vous avez besoin de voir. Cela prend une minute."],
      ["02", "Nous examinons", "Notre équipe lit chaque demande et répond par e-mail. Si votre entreprise travaille déjà avec nous, son administrateur peut vous ajouter directement."],
      ["03", "Invitation", "Une fois la demande acceptée, une invitation arrive par e-mail. Elle ouvre Ingabe dans l'espace de votre entreprise, avec ses cartes et ses travaux."],
      ["04", "Connexion", "Saisissez votre adresse e-mail et Ingabe vous envoie un code à usage unique. Aucun mot de passe à retenir, partager ou perdre."],
    ],
    request: "Demander l'accès",
    already: "Déjà invité ? Connectez-vous",
    boxTitle: "Comment la connexion protège votre travail",
    points: [
      ["Pas de mot de passe.", "Chaque connexion utilise un code à usage unique envoyé à votre adresse e-mail : il n'y a rien à voler d'un mot de passe réutilisé ou noté quelque part."],
      ["Un service de connexion dédié.", "La connexion passe par WorkOS, un service d'identité conçu pour les logiciels d'entreprise. Ingabe ne manipule jamais vos codes."],
      ["Chaque entreprise ne voit que son propre travail.", "Cartes, photos et résultats sont séparés dans la base de données elle-même, pas seulement à l'écran."],
      ["L'accès peut prendre fin immédiatement.", "L'administrateur de votre entreprise décide qui entre et peut retirer n'importe qui ; une personne retirée est déconnectée à son clic suivant."],
    ],
    waiting: "Vous attendez un code qui n'arrive pas ? Votre adresse n'a peut-être pas encore été invitée. La connexion n'envoie de codes qu'aux personnes invitées : demandez l'accès, ou appelez-nous au",
  },
  contact: {
    kicker: "Noza Labs",
    titleStart: "Dites-nous ce que vous avez besoin de voir",
    titleItalic: "depuis le ciel.",
    lead: "Exploitations, coopératives, assureurs, opérateurs de réseaux électriques et de tours : apportez un lieu et une question. Nous le survolerons et y répondrons avec vous.",
    call: "Appeler le",
  },
  footer: {
    mark: "Une seule marque sur tout ce que nous faisons, Ingabe compris : le n de noza, et un point.",
    readTitle: "Ce que nous lisons",
    readLinks: ["Carte des cultures", "Comptage des plants", "Pylônes et lignes"],
    appTitle: "Application Ingabe",
    appLinks: ["Ouvrir l'application", "Se connecter", "Demander l'accès", "Comment ça marche", "Comment fonctionne l'accès"],
    contact: "Contact",
    place: "Kigali, Rwanda",
    made: "Fait à Kigali",
  },
  drawing: {
    maize: "maïs",
    cassava: "manioc",
    beans: "haricots",
    "not sure · beans or maize?": "pas sûr · haricots ou maïs ?",
    "not sure · maize or beans?": "pas sûr · maïs ou haricots ?",
    "tower 2 · insulators": "pylône 2 · isolateurs",
    "tree close to line": "arbre proche de la ligne",
    "mast · antennas": "mât · antennes",
  },
};

export const DICTS: Record<Locale, Dict> = { en, fr };

/** The language a browser asks for first, from its Accept-Language header ("fr-SN,fr;q=0.9,en;q=0.8" -> "fr"). */
export function preferredLocale(acceptLanguage: string | null): Locale {
  if (!acceptLanguage) return "en";
  const ranked = acceptLanguage
    .split(",")
    .map((part) => {
      const [tag, ...params] = part.trim().split(";");
      const q = params.find((p) => p.trim().startsWith("q="));
      return { lang: tag.trim().toLowerCase().split("-")[0], q: q ? Number(q.trim().slice(2)) || 0 : 1 };
    })
    .filter((x) => x.lang && x.q > 0)
    .sort((a, b) => b.q - a.q);
  const first = ranked.find((x) => x.lang === "fr" || x.lang === "en");
  return first?.lang === "fr" ? "fr" : "en";
}
