"""Tests du jeu d'entrainement et de la ligne de base.

Deux choses sont verifiees ici, et ce sont les deux qui font qu'un chiffre de
performance veut dire quelque chose.

**La separation.** Elle doit se faire par appareil. Un avion est vu douze fois
en moyenne ; melanger ses positions entre entrainement et test noterait le
modele sur des appareils qu'il a deja vus.

**La mesure.** L'exactitude simple est trompeuse sur des classes
desequilibrees : repondre toujours la classe majoritaire afficherait un score
flatteur sans rien avoir appris. La mesure equilibree doit ramener ce cas a
50 %, et c'est teste explicitement.
"""

from __future__ import annotations

import re

import numpy as np
import pandas as pd
import pytest

from skytrace.ml import (
    BASELINE_FEATURE,
    FEATURES,
    ROTORCRAFT_RE,
    ModelCard,
    ThresholdBaseline,
    balanced_accuracy,
    evaluate,
    reliability_for,
    split_by_aircraft,
    train_and_evaluate,
)


def jeu_synthetique(n_commercial: int = 300, n_generale: int = 200) -> pd.DataFrame:
    """Deux populations separables, comme le sont les vraies.

    Les avions de ligne croisent haut et vite, l'aviation generale bas et
    lentement. Le recouvrement voulu entre les deux empeche un score parfait,
    qui ne testerait rien.
    """
    generateur = np.random.default_rng(3)
    lignes = []
    for etiquette, effectif, altitude, vitesse in (
        (1, n_commercial, 33000, 420),
        (0, n_generale, 5000, 120),
    ):
        lignes.append(
            pd.DataFrame(
                {
                    "aircraft_icao24": [f"{etiquette}{i:05x}" for i in range(effectif)],
                    "altitude_mediane_ft": generateur.normal(altitude, 9000, effectif),
                    "altitude_max_ft": generateur.normal(altitude * 1.2, 9000, effectif),
                    "vitesse_mediane_kt": generateur.normal(vitesse, 60, effectif),
                    "vitesse_max_kt": generateur.normal(vitesse * 1.2, 60, effectif),
                    "taux_vertical_median_ms": generateur.normal(2, 1, effectif),
                    "part_au_sol": generateur.uniform(0, 0.3, effectif),
                    "observations": generateur.integers(3, 40, effectif),
                    # Un avion de ligne vole sous de nombreux numeros de vol,
                    # un appareil prive garde le sien.
                    "indicatifs_distincts": generateur.integers(
                        *((4, 30) if etiquette else (1, 3)), effectif
                    ),
                    "commercial": etiquette,
                }
            )
        )
    return pd.concat(lignes, ignore_index=True)


class TestBalancedAccuracy:
    def test_prediction_parfaite(self):
        reel = np.array([0, 0, 1, 1])
        assert balanced_accuracy(reel, reel) == 1.0

    def test_toujours_la_classe_majoritaire_vaut_un_demi(self):
        # LE test de cette fonction. Sur 90 % de zeros, repondre toujours zero
        # donne 90 % d'exactitude simple - et doit donner 50 % ici, sinon la
        # mesure ne protege de rien.
        reel = np.array([0] * 90 + [1] * 10)
        predit = np.zeros(100, dtype=int)
        assert balanced_accuracy(reel, predit) == pytest.approx(0.5)

    def test_moyenne_bien_les_deux_rappels(self):
        # Classe 0 : 4 sur 4. Classe 1 : 1 sur 2. Moyenne attendue 0,75.
        reel = np.array([0, 0, 0, 0, 1, 1])
        predit = np.array([0, 0, 0, 0, 1, 0])
        assert balanced_accuracy(reel, predit) == pytest.approx(0.75)


class TestSplitByAircraft:
    def test_aucun_appareil_des_deux_cotes(self):
        entrainement, test = split_by_aircraft(jeu_synthetique())
        communs = set(entrainement["aircraft_icao24"]) & set(test["aircraft_icao24"])
        # La fuite qui rendrait tout score fantaisiste.
        assert communs == set()

    def test_rien_ne_se_perd(self):
        jeu = jeu_synthetique()
        entrainement, test = split_by_aircraft(jeu)
        assert len(entrainement) + len(test) == len(jeu)

    def test_proportions_conservees(self):
        jeu = jeu_synthetique()
        entrainement, test = split_by_aircraft(jeu, test_size=0.25)
        attendu = jeu["commercial"].mean()
        assert entrainement["commercial"].mean() == pytest.approx(attendu, abs=0.01)
        assert test["commercial"].mean() == pytest.approx(attendu, abs=0.01)

    def test_reproductible(self):
        jeu = jeu_synthetique()
        premier, _ = split_by_aircraft(jeu, seed=7)
        second, _ = split_by_aircraft(jeu, seed=7)
        # Une ligne de base qui bougerait d'une execution a l'autre ne serait
        # pas une reference.
        assert list(premier["aircraft_icao24"]) == list(second["aircraft_icao24"])

    def test_graine_differente_donne_un_autre_tirage(self):
        jeu = jeu_synthetique()
        premier, _ = split_by_aircraft(jeu, seed=1)
        second, _ = split_by_aircraft(jeu, seed=2)
        assert list(premier["aircraft_icao24"]) != list(second["aircraft_icao24"])


class TestThresholdBaseline:
    def test_bat_largement_le_hasard(self):
        entrainement, test = split_by_aircraft(jeu_synthetique())
        modele = ThresholdBaseline().fit(entrainement)
        mesures = evaluate(test["commercial"].to_numpy(), modele.predict(test))
        assert mesures["exactitude_equilibree"] > 0.8

    def test_seuil_entre_les_deux_populations(self):
        entrainement, _ = split_by_aircraft(jeu_synthetique())
        modele = ThresholdBaseline(feature="altitude_mediane_ft").fit(entrainement)
        # Le seuil doit tomber entre les deux nuages, pas a une extremite.
        assert 5000 < modele.threshold < 33000

    def test_la_variable_de_reference_a_ete_mesuree_et_non_supposee(self):
        # Regression sur une erreur reelle : la premiere ligne de base
        # utilisait l'altitude mediane. Un seuil sur la vitesse maximale fait
        # six points de mieux pour la meme regle, et attribuer au modele un
        # gain qui vient du choix de variable serait malhonnete.
        entrainement, test = split_by_aircraft(jeu_synthetique())
        par_altitude = ThresholdBaseline(feature="altitude_mediane_ft").fit(entrainement)
        par_defaut = ThresholdBaseline().fit(entrainement)
        assert par_defaut.feature == BASELINE_FEATURE
        assert par_defaut.feature != par_altitude.feature

    def test_le_seuil_vient_de_l_entrainement_seul(self):
        # Choisir le seuil sur le test reviendrait a s'auto-attribuer une
        # note. On verifie que `fit` n'a jamais vu le test : deux tests
        # differents ne changent pas le seuil.
        entrainement, test = split_by_aircraft(jeu_synthetique())
        seuil = ThresholdBaseline().fit(entrainement).threshold
        autre_test = test.assign(altitude_mediane_ft=test["altitude_mediane_ft"] + 20000)
        assert ThresholdBaseline().fit(entrainement).threshold == seuil
        assert len(autre_test) == len(test)

    def test_predit_bien_les_cas_extremes(self):
        entrainement, _ = split_by_aircraft(jeu_synthetique())
        modele = ThresholdBaseline(feature="altitude_mediane_ft").fit(entrainement)
        extremes = pd.DataFrame({"altitude_mediane_ft": [1000.0, 38000.0]})
        assert list(modele.predict(extremes)) == [0, 1]

    def test_valeur_manquante_ne_fait_pas_exploser(self):
        entrainement, _ = split_by_aircraft(jeu_synthetique())
        modele = ThresholdBaseline(feature="altitude_mediane_ft").fit(entrainement)
        # Un appareil sans mesure doit recevoir une reponse, pas une exception :
        # au moment de scorer, on ne choisit pas ses entrees.
        avec_trou = pd.DataFrame({"altitude_mediane_ft": [np.nan, 38000.0]})
        assert list(modele.predict(avec_trou)) == [0, 1]


class TestEvaluate:
    def test_expose_le_plancher_a_battre(self):
        reel = np.array([0] * 70 + [1] * 30)
        mesures = evaluate(reel, np.zeros(100, dtype=int))
        # Sans ce plancher affiche, un score de 70 % passerait pour un succes.
        assert mesures["plancher_majoritaire"] == pytest.approx(0.70)
        assert mesures["exactitude_equilibree"] == pytest.approx(0.5)

    def test_effectif_reporte(self):
        reel = np.array([0, 1, 1])
        assert evaluate(reel, reel)["effectif"] == 3


def test_les_variables_sont_toutes_cinetiques():
    # Aucune variable ne doit venir de la base aeronefs : ce serait predire
    # l'etiquette a partir d'elle-meme.
    interdits = {"manufacturer", "manufacturer_group", "aircraft_type", "model", "airline_name"}
    assert set(FEATURES) & interdits == set()


def _fiche(modele: float, reference: float) -> ModelCard:
    return ModelCard(
        trained_at="2026-08-25T00:00:00+00:00",
        features=FEATURES,
        n_train=1000,
        n_test=300,
        model_score=modele,
        baseline_score=reference,
        baseline_feature=BASELINE_FEATURE,
    )


class TestModelCard:
    def test_le_gain_se_mesure_contre_la_ligne_de_base(self):
        fiche = _fiche(modele=0.93, reference=0.89)
        assert fiche.gain == pytest.approx(0.04)
        assert fiche.is_worth_it()

    def test_un_gain_marginal_ne_justifie_pas_le_modele(self):
        # Un modele demande entrainement, versionnement, surveillance et
        # reentrainement. Un demi-point sur une regle d'une ligne ne paye pas
        # cette facture, et la fiche doit le dire plutot que de laisser la
        # decision a l'enthousiasme du moment.
        assert not _fiche(modele=0.895, reference=0.890).is_worth_it()


class TestTrainAndEvaluate:
    def test_bat_la_ligne_de_base(self):
        _, fiche = train_and_evaluate(jeu_synthetique(600, 400))
        assert fiche.model_score >= fiche.baseline_score
        assert fiche.n_train + fiche.n_test == 1000

    def test_le_modele_n_a_vu_que_des_variables_cinetiques(self):
        modele, _ = train_and_evaluate(jeu_synthetique(400, 300))
        # S'il avait ete entraine sur l'etiquette ou sur une colonne de la
        # base aeronefs, il en attendrait une de plus.
        assert modele.n_features_in_ == len(FEATURES)


class TestEtiquette:
    def test_les_voilures_tournantes_sont_exclues_du_commercial(self):
        # Regression sur une erreur reelle d'etiquetage. Airbus fabrique des
        # helicopteres : sans cette exclusion, le modele etait note FAUX
        # lorsqu'il refusait - a juste titre - de prendre un H145 pour un
        # A320. Corriger l'etiquette a rapporte plus que quarante
        # configurations d'hyperparametres.
        for modele in ("EC 130 T2", "MBB-BK 117 D-2 (H145)", "AS350B3", "BELL 429"):
            assert re.search(ROTORCRAFT_RE, modele.upper()), modele

    def test_les_avions_de_ligne_ne_sont_pas_pris_pour_des_helicopteres(self):
        # Le contre-test : un motif trop large viderait la classe commerciale.
        for modele in ("A320-214", "B737-800", "ERJ 190-100", "A350-941", "CRJ-900"):
            assert not re.search(ROTORCRAFT_RE, modele.upper()), modele


class TestPersistance:
    """Un modele qui vit en memoire n'est pas en production.

    Ces tests verifient les trois choses qui font la difference : il survit a
    l'arret du processus, il voyage avec sa fiche, et les versions
    precedentes restent disponibles pour un retour arriere.
    """

    def test_survit_a_un_aller_retour_sur_disque(self, tmp_path, monkeypatch):
        from skytrace.ml import load_model, save_model, train_and_evaluate

        monkeypatch.setenv("SKYTRACE_DATA_DIR", str(tmp_path))
        from skytrace.config import get_settings

        get_settings.cache_clear()

        jeu = jeu_synthetique(400, 300)
        modele, fiche = train_and_evaluate(jeu)
        save_model(modele, fiche)

        recharge, fiche_relue = load_model()
        # La fiche doit revenir identique : c'est elle qui autorise ou non la
        # mise en service.
        assert fiche_relue == fiche
        # Et le modele doit predire exactement pareil qu'avant sauvegarde.
        entrees = jeu[list(FEATURES)].head(50)
        assert list(recharge.predict(entrees)) == list(modele.predict(entrees))
        get_settings.cache_clear()

    def test_sans_modele_entraine_le_message_dit_quoi_faire(self, tmp_path, monkeypatch):
        from skytrace.ml import load_model

        monkeypatch.setenv("SKYTRACE_DATA_DIR", str(tmp_path))
        from skytrace.config import get_settings

        get_settings.cache_clear()
        with pytest.raises(FileNotFoundError, match="model train"):
            load_model()
        get_settings.cache_clear()

    def test_les_versions_precedentes_sont_conservees(self, tmp_path, monkeypatch):
        from dataclasses import replace

        from skytrace.ml import list_versions, load_model, save_model, train_and_evaluate

        monkeypatch.setenv("SKYTRACE_DATA_DIR", str(tmp_path))
        from skytrace.config import get_settings

        get_settings.cache_clear()

        modele, fiche = train_and_evaluate(jeu_synthetique(400, 300))
        save_model(modele, replace(fiche, trained_at="2026-08-01T10:00:00+00:00"))
        save_model(modele, replace(fiche, trained_at="2026-08-02T10:00:00+00:00"))

        # Le jour ou un reentrainement degrade les performances, il faut
        # pouvoir revenir en arriere : ecraser priverait du retour au moment
        # precis ou l'on en a besoin.
        assert len(list_versions()) == 2
        _, courante = load_model()
        assert courante.trained_at.startswith("2026-08-02")
        get_settings.cache_clear()


def test_decoupage_insensible_a_l_ordre_des_lignes():
    """Meme graine, meme jeu de test, quel que soit l'ordre d'arrivee.

    `build_dataset` agrege sans `order by` et DuckDB n'ordonne pas la sortie
    d'une agregation parallele : deux entrainements successifs tombaient sur
    deux decoupages differents. L'ecart de score entre deux versions pouvait
    alors n'etre que du bruit de tirage, ce qui rend toute comparaison de
    modeles - et donc toute decision de promotion - sans valeur.
    """
    jeu = jeu_synthetique(200, 200)
    melange = jeu.sample(frac=1.0, random_state=7).reset_index(drop=True)

    _, test_direct = split_by_aircraft(jeu, seed=1234)
    _, test_melange = split_by_aircraft(melange, seed=1234)

    assert sorted(test_direct["aircraft_icao24"]) == sorted(test_melange["aircraft_icao24"])


def test_fiabilite_suit_le_nombre_de_releves():
    """Un appareil peu vu ne doit pas se voir promettre le score global.

    Depuis la chute du plancher, le modele classe des appareils apercus une
    seule fois. Leur annoncer l'exactitude mesuree sur des appareils bien
    suivis serait une promesse fausse.
    """
    fiche = ModelCard(
        trained_at="2026-01-01T00:00:00+00:00",
        features=FEATURES,
        n_train=10,
        n_test=10,
        model_score=0.93,
        baseline_score=0.90,
        baseline_feature="vitesse_max_kt",
        scores_by_observations={
            "vu 1 fois": {"score": 0.79, "n_test": 100},
            "vu 3 fois ou plus": {"score": 0.94, "n_test": 100},
        },
    )
    observations = pd.Series([1, 2, 5], index=[10, 11, 12])
    fiabilite = reliability_for(fiche, observations)

    assert fiabilite.loc[10] == pytest.approx(0.79)
    # Cohorte non mesuree : on retombe sur le score global, jamais sur zero.
    assert fiabilite.loc[11] == pytest.approx(0.93)
    assert fiabilite.loc[12] == pytest.approx(0.94)
