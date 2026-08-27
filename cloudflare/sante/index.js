/**
 * Expose UNIQUEMENT le document de sante du lac, et rien d'autre.
 *
 * POURQUOI CE WORKER PLUTOT QU'UN BUCKET PUBLIC. Rendre le bucket public
 * exposerait ses 111 objets, dont 35 Mo de donnees OpenSky brutes. Le
 * probleme n'est pas la confidentialite - ces donnees sont publiques - mais
 * la redistribution : consommer une API pour un projet et republier sa base
 * en miroir telechargeable ne sont pas la meme chose, et les conditions
 * d'OpenSky encadrent la seconde. Le seul fichier utile pese 297 octets.
 *
 * Le bucket reste donc PRIVE. Ce Worker le lit avec un acces interne, et ne
 * sert qu'une seule cle. Toute autre adresse repond 404 : ce n'est pas un
 * filtre qu'on peut contourner en devinant un chemin, c'est une liste
 * d'autorisation d'un seul element.
 *
 * CE QU'IL AJOUTE, ET QUI COMPTE PLUS QUE LE FILTRAGE. Un fichier statique
 * ne peut pas signaler sa propre peremption : si la collecte s'arrete, plus
 * personne ne le reecrit, il se fige sur son dernier contenu et continue
 * d'annoncer "OK". Un moniteur par mot-cle ne verrait jamais l'arret.
 *
 * Un Worker, lui, calcule AU MOMENT DE LA LECTURE. Il compare la date de
 * depot de l'objet a l'heure courante et repond 503 quand elle est trop
 * vieille. La peremption devient donc un code HTTP, et n'importe quel
 * moniteur de disponibilite - meme le plus rudimentaire - detecte enfin
 * l'arret de la collecte. C'est la piece qui manquait.
 *
 * DEUX QUESTIONS, DEUX SOURCES. "Le publieur tourne-t-il encore ?" se lit
 * dans `uploaded`, l'horodatage de depot tenu par R2 : constate par le
 * stockage, il ne peut pas mentir sur le fait qu'une ecriture a eu lieu.
 * "La collecte est-elle fraiche ?" se lit dans le champ `etat` du corps, que
 * seul l'ecrivain sait calculer puisqu'il a acces au lac entier.
 *
 * Les deux peuvent diverger, et le code HTTP doit porter les deux - c'est la
 * seule information que lit un moniteur de disponibilite.
 *
 * Deploiement : voir docs/surveillance-externe.md
 */

/** Cle unique servie. Tout le reste est refuse. */
const CLE = "sante.json";

/** Au-dela, la publication est consideree a l'arret (defaut : 10 heures). */
const SEUIL_HEURES_PAR_DEFAUT = 10;

/**
 * Jamais de cache. Un 200 mis en cache serait reservi apres l'arret de la
 * collecte, ce qui masquerait exactement la panne qu'on cherche a voir.
 */
const ENTETES = {
  "Content-Type": "application/json; charset=utf-8",
  "Cache-Control": "no-store, max-age=0",
};

function reponse(corps, statut) {
  return new Response(JSON.stringify(corps, null, 2), {
    status: statut,
    headers: ENTETES,
  });
}

export default {
  async fetch(requete, env) {
    const url = new URL(requete.url);

    if (requete.method !== "GET" && requete.method !== "HEAD") {
      return reponse({ erreur: "methode non autorisee" }, 405);
    }

    // La racine est un raccourci commode ; tout autre chemin n'existe pas.
    const chemin = url.pathname.replace(/^\/+/, "");
    if (chemin !== "" && chemin !== CLE) {
      return reponse({ erreur: "introuvable" }, 404);
    }

    const objet = await env.LAC.get(CLE);
    if (objet === null) {
      return reponse(
        {
          etat: "JAMAIS_PUBLIE",
          detail: "Aucun document de sante dans le lac. Lancer `skytrace publier-sante`.",
        },
        503,
      );
    }

    const seuil = Number(env.SEUIL_HEURES ?? SEUIL_HEURES_PAR_DEFAUT);
    const ageHeures = (Date.now() - objet.uploaded.getTime()) / 3600000;

    let corps;
    try {
      corps = JSON.parse(await objet.text());
    } catch {
      // Document illisible : c'est une panne, pas une absence de nouvelle.
      return reponse({ etat: "DOCUMENT_ILLISIBLE" }, 502);
    }

    // DEUX PANNES DISTINCTES, ET IL FAUT LES DEUX.
    //
    // `uploaded` dit si le PUBLIEUR tourne encore. Le champ `etat` du corps
    // dit si la COLLECTE est fraiche. Les deux peuvent diverger, et cette
    // divergence n'est pas theorique : elle s'est produite pendant l'ecriture
    // de ce Worker. La collecte etait arretee depuis 33 heures, mais un
    // `skytrace publier-sante` lance a la main venait de deposer un document
    // tout frais. Un Worker qui ne regarderait que `uploaded` aurait repondu
    // 200 sur un pipeline mort.
    //
    // Le code HTTP doit donc porter les deux : c'est la seule information que
    // lit un moniteur de disponibilite, et il n'ouvrira jamais le corps.
    const publicationArretee = ageHeures > seuil;
    const collecteArretee = corps.etat !== "OK";

    // Ce que le Worker constate s'ajoute a ce que l'ecrivain declarait, sans
    // l'ecraser : les deux points de vue restent lisibles cote a cote.
    corps.publication_il_y_a_heures = Math.round(ageHeures * 100) / 100;
    corps.seuil_publication_heures = seuil;
    corps.verdict = publicationArretee
      ? "PUBLICATION_A_L_ARRET"
      : collecteArretee
        ? "COLLECTE_A_L_ARRET"
        : "TOUT_VA_BIEN";

    return reponse(corps, publicationArretee || collecteArretee ? 503 : 200);
  },
};
