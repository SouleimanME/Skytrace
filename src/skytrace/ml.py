"""Combler les trous de la base aeronefs a partir du comportement en vol.

LE PROBLEME. Sur 67 867 appareils observes, 26 028 - soit 38 % - n'ont aucun
constructeur dans la base OpenSky. La fiche appareil du tableau de bord
affiche alors "Constructeur : Inconnu". Ces appareils volent pourtant, et
leur facon de voler dit beaucoup.

LA CIBLE, CHOISIE D'APRES CE QUE LA DONNEE SOUTIENT. On ne predit PAS le
constructeur. Mesure faite sur les appareils etiquetes :

    Boeing     34 000 ft   433 kt
    Airbus     32 975 ft   417 kt
    Embraer    24 950 ft   373 kt
    Autre       4 500 ft   119 kt

Un A320 et un 737 volent de la meme facon - separer Airbus de Boeing
reviendrait a apprendre du bruit. En revanche, transport commercial contre
aviation generale, c'est un facteur sept en altitude et trois en vitesse.
C'est cette question-la que l'on pose, parce que c'est celle a laquelle la
donnee peut repondre.

L'AUTRE ETIQUETTE QU'ON AURAIT PREFEREE. La base porte une colonne
`categoryDescription` (Light, Large, Heavy, Rotorcraft) qui serait une cible
ideale. Elle est vide pour 96 % des appareils observes : inutilisable.

LA LIMITE DE L'ETIQUETTE RETENUE. Airbus fabrique aussi des helicopteres, et
8,1 % des appareils etiquetes "Airbus" volent sous 10 000 ft de mediane -
voilures tournantes et appareils vus seulement en approche. L'etiquette est
donc legerement bruitee, ce qui borne par le haut ce qu'un modele peut
atteindre.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

#: Constructeurs de transport commercial. Le reste des constructeurs connus
#: est de l'aviation generale ou d'affaires.
COMMERCIAL = ("Airbus", "Boeing", "Embraer", "Bombardier", "ATR")

#: Nombre minimal de releves pour qu'un appareil soit classe.
#:
#: Ce plancher etait a trois, au motif qu'une mediane sur moins de trois
#: points n'est pas une mediane. Le raisonnement etait juste, la conclusion
#: mauvaise : il ecartait 43 % de la flotte - 32 581 appareils vus une ou
#: deux fois - pour proteger sept dixiemes de point sur une majorite deja
#: a 0.94.
#:
#: Mesure, a decoupage identique : entraine sur tout plutot que sur le seul
#: sous-ensemble bien observe, le modele passe de 0.635 a 0.781 sur les
#: appareils vus une fois, de 0.743 a 0.815 sur ceux vus deux fois, et ne
#: cede que 0.942 -> 0.935 sur les autres. En appareils correctement
#: classes sur la flotte reelle : 40 451 -> 66 042.
#:
#: Le refus n'etait donc pas de la prudence, c'etait de l'abstention. Un
#: appareil peu vu est desormais classe ET signale comme tel, sa fiabilite
#: reelle etant portee par `ModelCard.scores_by_observations`. Dire "je ne
#: sais pas bien" informe ; ne rien dire n'informe pas.
MIN_OBSERVATIONS = 1

#: Cohortes de fiabilite, par nombre de releves. Un score global masque un
#: ecart de seize points entre un appareil vu une fois et un appareil bien
#: suivi : le publier seul reviendrait a promettre la meme chose aux deux.
OBSERVATION_COHORTS = ((1, 1, "vu 1 fois"), (2, 2, "vu 2 fois"), (3, None, "vu 3 fois ou plus"))

#: Familles a voilure tournante, reperees par leur designation. Airbus
#: fabrique des helicopteres, Bombardier et Embraer des jets d'affaires : le
#: constructeur seul ne dit pas si un appareil est un avion de ligne. Sans
#: cette correction, le modele etait note FAUX quand il refusait, a juste
#: titre, de prendre un H145 pour un A320.
#:
#: Liste curatee, donc a maintenir. C'est le prix d'une etiquette juste.
ROTORCRAFT_PATTERNS = (
    "EC ?1",
    "AS ?3",
    "AS ?5",
    "MBB",
    "BK ?117",
    "H1[2346]5",
    "SA3",
    "A109",
    "AW1",
    "R4[46]",
    "R66",
    "S-?(76|92)",
    "BELL",
    "B0[6]",
    "B429",
)

FEATURES = (
    "altitude_mediane_ft",
    "altitude_max_ft",
    "vitesse_mediane_kt",
    "vitesse_max_kt",
    "taux_vertical_median_ms",
    "part_au_sol",
    "observations",
    # Un avion de ligne vole sous des dizaines de numeros de vol differents ;
    # un appareil prive garde le meme indicatif, derive de son
    # immatriculation. Cette seule variable apporte +0,013 - plus que les six
    # autres candidates testees reunies. Elle reste une observation et non une
    # donnee du referentiel : aucune fuite.
    "indicatifs_distincts",
)

ROTORCRAFT_RE = "|".join(ROTORCRAFT_PATTERNS)

# Les valeurs interpolees ici sont des CONSTANTES du module - la liste des
# constructeurs, l'expression des voilures tournantes, le plancher
# d'observation. Aucune entree exterieure n'y parvient, et un identifiant
# SQL ne peut de toute facon pas etre lie comme un parametre.
DATASET_SQL = f"""
    with vol as (
        select
            p.aircraft_icao24,
            median(p.barometric_altitude_ft)            as altitude_mediane_ft,
            max(p.barometric_altitude_ft)               as altitude_max_ft,
            median(p.ground_speed_kt)                   as vitesse_mediane_kt,
            max(p.ground_speed_kt)                      as vitesse_max_kt,
            median(abs(p.vertical_rate_ms))             as taux_vertical_median_ms,
            avg(case when p.is_on_ground then 1.0 else 0.0 end) as part_au_sol,
            count(*)                                    as observations,
            count(distinct p.callsign)                  as indicatifs_distincts
        from marts.fct_aircraft_positions p
        -- Les positions perimees decriraient un vol qui n'a pas eu lieu.
        where not p.is_position_stale
        group by 1
        having count(*) >= {MIN_OBSERVATIONS}
    )
    select
        vol.*,
        a.manufacturer_group,
        case
            -- Une voilure tournante n'est pas un avion de ligne, quel que
            -- soit son constructeur.
            when regexp_matches(upper(coalesce(a.model, '')), '{ROTORCRAFT_RE}') then 0
            when a.manufacturer_group in {COMMERCIAL} then 1
            else 0
        end as commercial
    from vol
    join marts.dim_aircraft a using (aircraft_icao24)
    where a.manufacturer_group <> 'Inconnu'
"""  # noqa: S608


def build_dataset(connection) -> pd.DataFrame:
    """Un appareil, une ligne : ses statistiques de vol et son etiquette.

    L'agregation par appareil n'est pas un detail de commodite. Les positions
    d'un meme avion ne sont pas des observations independantes - elles
    decrivent le meme appareil - et travailler ligne par ligne ferait croire a
    des dizaines de milliers d'exemples la ou il y en a quelques dizaines de
    milliers... d'appareils.
    """
    frame = connection.execute(DATASET_SQL).df()
    return frame.dropna(subset=["altitude_mediane_ft", "vitesse_mediane_kt"])


def split_by_aircraft(
    frame: pd.DataFrame, *, test_size: float = 0.25, seed: int = 20260825
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Separe ENTRAINEMENT et TEST par appareil, jamais par observation.

    C'est le piege central de ce jeu de donnees. Un appareil est vu douze fois
    en moyenne : un decoupage aleatoire des positions mettrait le meme Boeing
    des deux cotes, et le modele serait note sur des avions qu'il a deja vus.
    Le score serait magnifique et faux.

    Ici chaque ligne EST deja un appareil, donc le decoupage est correct par
    construction - mais la fonction existe pour que ce choix soit explicite et
    testable plutot que tacite.

    Le tirage est stratifie : les deux classes gardent leur proportion, sinon
    un tirage malheureux fausserait la comparaison.

    Le tri prealable n'est pas cosmetique. `permutation` melange des
    POSITIONS : a graine egale, deux ordres d'entree donnent deux decoupages
    differents. Or `build_dataset` agrege sans `order by`, et DuckDB n'ordonne
    pas la sortie d'une agregation parallele. Deux entrainements successifs
    sur la meme donnee ne tombaient donc pas sur le meme jeu de test, et
    l'ecart constate entre deux versions pouvait n'etre que du bruit de
    tirage. Trier par adresse OACI rend la graine reellement suffisante.
    """
    frame = frame.sort_values("aircraft_icao24", kind="stable").reset_index(drop=True)
    generateur = np.random.default_rng(seed)
    entrainement, test = [], []
    for _, groupe in frame.groupby("commercial", sort=True):
        melange = groupe.iloc[generateur.permutation(len(groupe))]
        coupe = int(round(len(melange) * test_size))
        test.append(melange.iloc[:coupe])
        entrainement.append(melange.iloc[coupe:])
    return (
        pd.concat(entrainement).reset_index(drop=True),
        pd.concat(test).reset_index(drop=True),
    )


#: Variable de la ligne de base. Ce choix a ete MESURE, pas suppose, et la
#: mesure a corrige une premiere intuition : un seuil sur l'altitude mediane
#: plafonne a 0,828, un seuil sur la vitesse maximale atteint 0,892. Six
#: points d'ecart pour la meme regle d'une ligne, simplement en regardant la
#: bonne colonne.
#:
#: La consequence porte sur ce qu'on a le droit d'attribuer au modele. Compare
#: a la premiere reference, il gagnait dix points ; compare a la bonne, il en
#: gagne trois et demi. C'est le second chiffre qui est honnete.
BASELINE_FEATURE = "vitesse_max_kt"


@dataclass
class ThresholdBaseline:
    """Ligne de base : un seuil, sur une seule variable.

    Sans point de comparaison, un score n'a aucun sens. Un modele a 92 % est
    excellent si la reference est a 60 %, et discutable si une regle d'une
    ligne fait 89 % - ce qui est precisement le cas ici.

    Le seuil est CHOISI SUR L'ENTRAINEMENT et applique tel quel au test. Le
    choisir sur le test reviendrait a s'auto-attribuer une note.
    """

    feature: str = BASELINE_FEATURE
    threshold: float = 0.0

    def fit(self, frame: pd.DataFrame) -> ThresholdBaseline:
        valeurs = frame[self.feature].to_numpy(float)
        cible = frame["commercial"].to_numpy(int)
        connues = ~np.isnan(valeurs)
        # On balaye les centiles plutot qu'une grille arbitraire : le seuil
        # reste ainsi dans la plage reellement observee.
        candidats = np.unique(np.quantile(valeurs[connues], np.linspace(0.01, 0.99, 99)))
        scores = [
            balanced_accuracy(cible[connues], (valeurs[connues] >= seuil).astype(int))
            for seuil in candidats
        ]
        self.threshold = float(candidats[int(np.argmax(scores))])
        return self

    def predict(self, frame: pd.DataFrame) -> np.ndarray:
        valeurs = np.nan_to_num(frame[self.feature].to_numpy(float), nan=-np.inf)
        return (valeurs >= self.threshold).astype(int)


def balanced_accuracy(reel: np.ndarray, predit: np.ndarray) -> float:
    """Moyenne des taux de bonne detection de chaque classe.

    L'exactitude simple ne convient pas : l'aviation generale est majoritaire,
    et un modele qui repondrait toujours "generale" afficherait un score
    flatteur sans rien avoir appris. La mesure equilibree le ramene a 50 %.
    """
    rappels = []
    for classe in (0, 1):
        presents = reel == classe
        if presents.sum() == 0:
            continue
        rappels.append(float((predit[presents] == classe).mean()))
    return float(np.mean(rappels)) if rappels else float("nan")


def evaluate(reel: np.ndarray, predit: np.ndarray) -> dict[str, float]:
    """Ce qu'il faut regarder, y compris le plancher a battre."""
    reel, predit = np.asarray(reel, int), np.asarray(predit, int)
    majoritaire = int(np.bincount(reel).argmax())
    return {
        "exactitude": float((predit == reel).mean()),
        "exactitude_equilibree": balanced_accuracy(reel, predit),
        "rappel_commercial": float(predit[reel == 1].mean()) if (reel == 1).any() else float("nan"),
        "rappel_generale": float(
            (predit[reel == 0] == 0).mean() if (reel == 0).any() else float("nan")
        ),
        # Le plancher : repondre systematiquement la classe majoritaire.
        "plancher_majoritaire": float((reel == majoritaire).mean()),
        "effectif": int(len(reel)),
    }


#: Reglages du modele. HistGradientBoosting est retenu pour deux raisons
#: concretes, pas par mode : il traite les valeurs manquantes nativement - un
#: appareil sans taux vertical recoit quand meme une prediction, ce qui compte
#: au moment de scorer - et il fait jeu egal avec une foret aleatoire (0,9329
#: contre 0,9324) sans imputation a maintenir.
#: Une recherche aleatoire sur 40 configurations - taux d'apprentissage,
#: profondeur, regularisation, echantillonnage - n'a RIEN gagne : -0,0002 sur
#: le test, soit du bruit. Le modele n'est pas limite par ses reglages, il
#: l'est par ses variables et par la qualite de son etiquette. Les deux gains
#: reels sont venus de la : +0,013 en ajoutant les indicatifs distincts,
#: +0,004 en corrigeant l'etiquette des voilures tournantes.
#:
#: On garde donc des reglages simples et lisibles plutot qu'un jeu de
#: parametres optimises a la troisieme decimale sur un seul decoupage.
MODEL_PARAMS = {
    "max_iter": 300,
    "learning_rate": 0.08,
    # Les classes sont desequilibrees (59 % de commercial). Sans ce
    # reequilibrage, le modele optimiserait l'exactitude simple et
    # negligerait l'aviation generale, qui est justement la classe que la
    # ligne de base ratait.
    "class_weight": "balanced",
    "random_state": 20260825,
}


def train_model(entrainement: pd.DataFrame):
    """Entraine le classifieur sur les variables cinetiques.

    Aucune variable ne vient de la base aeronefs : ce serait predire
    l'etiquette a partir d'elle-meme.
    """
    from sklearn.ensemble import HistGradientBoostingClassifier

    modele = HistGradientBoostingClassifier(**MODEL_PARAMS)
    modele.fit(entrainement[list(FEATURES)], entrainement["commercial"].to_numpy(int))
    return modele


@dataclass(frozen=True)
class ModelCard:
    """Ce qu'il faut savoir d'un modele avant de s'en servir.

    Un fichier de modele seul ne dit ni sur quoi il a ete entraine, ni ce
    qu'il valait, ni contre quoi il a ete compare. Ces trois informations
    voyagent donc avec lui : sans elles, on ne peut ni decider de le
    promouvoir, ni diagnostiquer sa derive six mois plus tard.
    """

    trained_at: str
    features: tuple[str, ...]
    n_train: int
    n_test: int
    model_score: float
    baseline_score: float
    baseline_feature: str
    #: Mediane de chaque variable au moment de l'entrainement, et part de la
    #: classe commerciale. Ces reperes sont la SEULE facon de detecter plus
    #: tard que le monde a bouge : sans eux, on ne peut comparer la donnee
    #: d'aujourd'hui qu'a elle-meme, ce qui ne dit rien.
    feature_medians: dict[str, float] = field(default_factory=dict)
    commercial_share: float = 0.0
    #: Exactitude equilibree par cohorte d'observation, et effectif de test
    #: correspondant. Depuis la chute du plancher, le modele classe des
    #: appareils vus une seule fois : leur annoncer le score global serait
    #: leur promettre une fiabilite qu'ils n'ont pas.
    scores_by_observations: dict[str, dict] = field(default_factory=dict)

    @property
    def gain(self) -> float:
        """Ce que le modele apporte VRAIMENT, au-dela de la regle d'une ligne."""
        return self.model_score - self.baseline_score

    def is_worth_it(self, *, minimum: float = 0.02) -> bool:
        """Le modele merite-t-il son cout d'exploitation ?

        Un modele demande a etre entraine, versionne, surveille et reentraine.
        S'il ne gagne qu'un point sur une regle d'une ligne, la regle vaut
        mieux. Le seuil est explicite plutot que laisse au jugement du moment.
        """
        return self.gain >= minimum


def train_and_evaluate(jeu: pd.DataFrame, *, seed: int = 20260825) -> tuple[object, ModelCard]:
    """Entraine, compare a la ligne de base, et rend le modele avec sa fiche.

    La comparaison se fait sur le MEME decoupage : comparer deux scores issus
    de deux tirages differents ne compare rien.
    """
    from datetime import UTC, datetime

    entrainement, test = split_by_aircraft(jeu, seed=seed)
    cible = test["commercial"].to_numpy(int)

    reference = ThresholdBaseline().fit(entrainement)
    modele = train_model(entrainement)

    # Meme modele, meme jeu de test, decoupe par nombre de releves : c'est
    # la seule facon d'annoncer a chaque appareil ce qu'il vaut vraiment.
    predit = modele.predict(test[list(FEATURES)])
    cohortes = {}
    for plancher, plafond, libelle in OBSERVATION_COHORTS:
        dans = test["observations"] >= plancher
        if plafond is not None:
            dans &= test["observations"] <= plafond
        # Sous une trentaine d'appareils, le score tient du hasard : mieux
        # vaut ne rien annoncer qu'annoncer un chiffre instable.
        if int(dans.sum()) < 30:
            continue
        cohortes[libelle] = {
            "score": evaluate(cible[dans.to_numpy()], predit[dans.to_numpy()])[
                "exactitude_equilibree"
            ],
            "n_test": int(dans.sum()),
        }

    return modele, ModelCard(
        trained_at=datetime.now(UTC).isoformat(timespec="seconds"),
        features=FEATURES,
        n_train=len(entrainement),
        n_test=len(test),
        model_score=evaluate(cible, modele.predict(test[list(FEATURES)]))["exactitude_equilibree"],
        baseline_score=evaluate(cible, reference.predict(test))["exactitude_equilibree"],
        baseline_feature=reference.feature,
        feature_medians={variable: float(entrainement[variable].median()) for variable in FEATURES},
        commercial_share=float(entrainement["commercial"].mean()),
        scores_by_observations=cohortes,
    )


# ---------------------------------------------------------------------------
# Persistance
# ---------------------------------------------------------------------------
#: Nom du modele. Un projet finit par en avoir plusieurs ; les nommer des le
#: premier evite d'avoir a tout renommer au second.
MODEL_NAME = "aircraft_class"


def model_dir(settings=None):
    """Repertoire des modeles, a cote du lac et de l'entrepot."""
    from skytrace.config import get_settings

    settings = settings or get_settings()
    #  peut etre nul quand la configuration s'en remet au defaut ;
    #  porte toujours le chemin effectif.
    chemin = settings.resolved_data_dir / "models"
    chemin.mkdir(parents=True, exist_ok=True)
    return chemin


def save_model(modele, card: ModelCard, settings=None) -> Path:
    """Enregistre le modele DATE, et le designe comme version courante.

    Deux fichiers par version : le modele et sa fiche. Et un pointeur
    `current.json` qui dit laquelle sert.

    Pourquoi dater plutot qu'ecraser : le jour ou un reentrainement degrade
    les performances, il faut pouvoir revenir a la version precedente sans
    l'avoir perdue. Ecraser, c'est se priver du retour arriere au moment
    precis ou l'on en a besoin.
    """
    import json

    import joblib

    dossier = model_dir(settings)
    horodatage = card.trained_at.replace(":", "").replace("-", "")[:15]
    base = dossier / f"{MODEL_NAME}-{horodatage}"

    joblib.dump(modele, base.with_suffix(".joblib"))
    base.with_suffix(".json").write_text(
        json.dumps(asdict(card), indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (dossier / "current.json").write_text(
        json.dumps({"version": base.name}, indent=2), encoding="utf-8"
    )
    return base.with_suffix(".joblib")


def load_model(settings=None) -> tuple[object, ModelCard]:
    """Charge la version courante, avec sa fiche.

    Le modele et sa fiche voyagent ensemble : un modele dont on ignore la
    date, les variables et le score ne devrait pas etre mis en service.
    """
    import json

    import joblib

    dossier = model_dir(settings)
    pointeur = dossier / "current.json"
    if not pointeur.exists():
        raise FileNotFoundError(
            f"Aucun modele entraine dans {dossier}. Lancer : skytrace model train"
        )
    version = json.loads(pointeur.read_text(encoding="utf-8"))["version"]
    fiche = json.loads((dossier / f"{version}.json").read_text(encoding="utf-8"))
    fiche["features"] = tuple(fiche["features"])
    return joblib.load(dossier / f"{version}.joblib"), ModelCard(**fiche)


def list_versions(settings=None) -> list[str]:
    """Toutes les versions enregistrees, de la plus recente a la plus ancienne."""
    return sorted(
        (p.stem for p in model_dir(settings).glob(f"{MODEL_NAME}-*.joblib")), reverse=True
    )


#: Statistiques de vol d'appareils DESIGNES, pour les scorer a la demande.
#: Meme calcul que le jeu d'entrainement - il doit le rester, sans quoi le
#: modele verrait a l'usage des variables construites autrement qu'a
#: l'entrainement.
# Meme situation que `DATASET_SQL` : seules des constantes du module sont
# interpolees. Les adresses OACI, elles, sont LIEES dans `predict_aircraft`.
PREDICT_SQL = f"""
    with vol as (
        select
            p.aircraft_icao24,
            median(p.barometric_altitude_ft)            as altitude_mediane_ft,
            max(p.barometric_altitude_ft)               as altitude_max_ft,
            median(p.ground_speed_kt)                   as vitesse_mediane_kt,
            max(p.ground_speed_kt)                      as vitesse_max_kt,
            median(abs(p.vertical_rate_ms))             as taux_vertical_median_ms,
            avg(case when p.is_on_ground then 1.0 else 0.0 end) as part_au_sol,
            count(*)                                    as observations,
            count(distinct p.callsign)                  as indicatifs_distincts
        from marts.fct_aircraft_positions p
        where not p.is_position_stale
          and p.aircraft_icao24 in ({{adresses}})
        group by 1
        having count(*) >= {MIN_OBSERVATIONS}
    )
    select vol.*, a.manufacturer_group, a.registration, a.model,
           a.most_frequent_callsign
    from vol
    left join marts.dim_aircraft a using (aircraft_icao24)
"""  # noqa: S608


def _est_adresse_oaci(valeur: str) -> bool:
    """Une adresse OACI 24 bits : six caracteres hexadecimaux, et rien d'autre."""
    return len(valeur) == 6 and all(c in "0123456789abcdefABCDEF" for c in valeur)


def predict_aircraft(connection, modele, icao24: list[str]) -> pd.DataFrame:
    """Score des appareils designes par leur adresse OACI.

    Renvoie la classe predite ET sa probabilite. Une prediction sans son
    degre de certitude invite a la prendre pour un fait.
    """
    # DEUX PROTECTIONS, ET ELLES NE FONT PAS DOUBLON.
    #
    # Ces adresses viennent de la ligne de commande, donc de l'exterieur. La
    # version precedente les inserait telles quelles dans la requete par
    # interpolation de chaine : une valeur contenant une apostrophe cassait
    # la requete, et une valeur choisie pouvait en changer le sens.
    #
    # Le FILTRE ecarte ce qui n'est pas une adresse OACI - six caracteres
    # hexadecimaux, rien d'autre - et rend donc l'erreur de frappe visible
    # plutot que silencieuse. La LIAISON garantit que le contenu reste une
    # valeur et ne devienne jamais de la syntaxe, quoi qu'il traverse le
    # filtre un jour.
    valides = [a.lower() for a in icao24 if a and _est_adresse_oaci(a)]
    if not valides:
        return pd.DataFrame()
    marqueurs = ", ".join("?" for _ in valides)
    frame = connection.execute(PREDICT_SQL.format(adresses=marqueurs), valides).df()
    if frame.empty:
        return frame
    proba = modele.predict_proba(frame[list(FEATURES)])[:, 1]
    return frame.assign(
        probabilite_commercial=proba,
        classe_predite=np.where(proba >= 0.5, "transport commercial", "aviation generale"),
    )


#: Tous les appareils assez observes, etiquetes ou non. Le scoring couvre les
#: DEUX : sur ceux dont on connait le constructeur, la prediction sert de
#: controle permanent - on peut comparer a la verite ; sur les autres, elle
#: comble le trou.
SCORE_SQL = PREDICT_SQL.replace("and p.aircraft_icao24 in ({adresses})", "").replace(
    "PREDICT", "SCORE"
)


def reliability_for(card: ModelCard, observations) -> pd.Series:
    """Exactitude annoncable pour chaque appareil, selon combien il a ete vu.

    Sans plancher, la table melange des appareils vus une fois et des
    appareils suivis pendant des jours. Leur servir le meme score serait
    exact en moyenne et faux pour chacun.
    """
    fiabilite = pd.Series(card.model_score, index=observations.index, dtype=float)
    for plancher, plafond, libelle in OBSERVATION_COHORTS:
        mesure = card.scores_by_observations.get(libelle)
        if not mesure:
            continue
        dans = observations >= plancher
        if plafond is not None:
            dans &= observations <= plafond
        fiabilite.loc[dans] = mesure["score"]
    return fiabilite


def score_all(connection, modele, card: ModelCard) -> pd.DataFrame:
    """Score tous les appareils observes, connus comme inconnus."""
    frame = connection.execute(SCORE_SQL).df()
    proba = modele.predict_proba(frame[list(FEATURES)])[:, 1]
    return pd.DataFrame(
        {
            "aircraft_icao24": frame["aircraft_icao24"],
            "predicted_commercial": (proba >= 0.5).astype(int),
            "probability_commercial": proba,
            # La version voyage avec chaque ligne : sans elle, impossible de
            # savoir six mois plus tard quel modele a produit quel score, ni
            # d'imputer une derive au bon coupable.
            "model_trained_at": card.trained_at,
            "model_score": card.model_score,
            # Combien de fois l'appareil a ete vu, et ce que le modele vaut
            # REELLEMENT sur les appareils vus autant de fois. Sans ces deux
            # colonnes, le tableau de bord promettrait 0.94 a un appareil
            # apercu une seule fois, pour lequel il vaut 0.78.
            "observations": frame["observations"].astype(int),
            "score_for_this_aircraft": reliability_for(card, frame["observations"]),
            # Repere d'entrainement embarque avec chaque ligne. Le tableau de
            # bord deploye n'a pas acces au fichier du modele - le conteneur
            # est neuf a chaque reveil - donc sans cette colonne il ne
            # pourrait comparer la donnee d'aujourd'hui qu'a elle-meme, ce qui
            # ne dit rien d'une derive.
            "training_commercial_share": card.commercial_share,
            "scored_at": pd.Timestamp.now(tz="UTC"),
        }
    )
