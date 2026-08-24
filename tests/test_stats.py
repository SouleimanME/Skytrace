"""Tests de l'estimation d'incertitude sur donnees groupees.

Un module numerique se teste sur des cas ou l'on connait la reponse a
l'avance, pas sur la donnee reelle - dont on ne sait justement pas ce qu'elle
devrait dire. On fabrique donc des panels dont on a choisi la structure, y
compris un paradoxe de Simpson construit de toutes pieces.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from skytrace.stats import estimate, within_correlation

RAPIDE = {"iterations": 300}


def panel_simpson(n_groupes: int = 12, par_groupe: int = 50) -> pd.DataFrame:
    """Panel ou la correlation s'inverse quand on controle le groupe.

    Chaque groupe a son propre niveau : les groupes a fort x ont aussi un
    fort y, ce qui cree une correlation POSITIVE entre groupes. A l'interieur
    d'un groupe, la relation est NEGATIVE. C'est exactement la structure du
    resultat que le projet publie.
    """
    generateur = np.random.default_rng(7)
    lignes = []
    for groupe in range(n_groupes):
        niveau = groupe * 10.0
        x = niveau + generateur.normal(0, 1, par_groupe)
        y = niveau + (-0.9) * (x - niveau) + generateur.normal(0, 0.3, par_groupe)
        lignes.append(pd.DataFrame({"groupe": f"G{groupe}", "x": x, "y": y}))
    return pd.concat(lignes, ignore_index=True)


def panel_pentes(pentes: list[float], par_groupe: int = 40) -> pd.DataFrame:
    """Panel ou l'on choisit la pente intra de CHAQUE groupe.

    Sert a comparer un effet homogene entre groupes a un effet heterogene :
    c'est cette heterogeneite qui decide si le regroupement elargit ou non
    l'intervalle.
    """
    generateur = np.random.default_rng(5)
    lignes = []
    for i, pente in enumerate(pentes):
        niveau = i * 10.0
        x = niveau + generateur.normal(0, 1, par_groupe)
        y = niveau + pente * (x - niveau) + generateur.normal(0, 1, par_groupe)
        lignes.append(pd.DataFrame({"groupe": f"G{i}", "x": x, "y": y}))
    return pd.concat(lignes, ignore_index=True)


def panel_bruit(n_groupes: int = 12, par_groupe: int = 50) -> pd.DataFrame:
    """Panel sans aucun lien : y est du bruit pur."""
    generateur = np.random.default_rng(11)
    lignes = []
    for groupe in range(n_groupes):
        lignes.append(
            pd.DataFrame(
                {
                    "groupe": f"G{groupe}",
                    "x": generateur.normal(groupe, 1, par_groupe),
                    "y": generateur.normal(0, 1, par_groupe),
                }
            )
        )
    return pd.concat(lignes, ignore_index=True)


class TestWithinCorrelation:
    def test_inverse_bien_le_signe_de_la_correlation_brute(self):
        panel = panel_simpson()
        brute = panel["x"].corr(panel["y"])
        intra = within_correlation(panel, "x", "y", "groupe")

        # Le piege que l'estimateur doit detecter : brute positive, intra
        # negative. Si les deux avaient le meme signe, le test ne verifierait
        # rien d'interessant.
        assert brute > 0.9
        assert intra < -0.9

    def test_panel_vide(self):
        vide = pd.DataFrame({"x": [], "y": [], "groupe": []})
        assert np.isnan(within_correlation(vide, "x", "y", "groupe"))

    def test_ignore_les_lignes_incompletes(self):
        panel = panel_simpson(n_groupes=4, par_groupe=20)
        avec_trous = panel.copy()
        avec_trous.loc[avec_trous.index[:10], "y"] = np.nan
        # Le resultat doit rester du meme ordre : les trous sont ecartes, pas
        # remplaces par zero - ce qui deformerait la correlation.
        assert within_correlation(avec_trous, "x", "y", "groupe") < -0.8


class TestEstimate:
    def test_intervalle_encadre_l_estimation(self):
        res = estimate(panel_simpson(), "x", "y", "groupe", **RAPIDE)
        assert res.low <= res.correlation <= res.high
        assert res.clusters == 12
        assert res.observations == 600

    def test_lien_reel_detecte(self):
        res = estimate(panel_simpson(), "x", "y", "groupe", **RAPIDE)
        assert res.excludes_zero
        assert res.p_value < 0.05

    def test_bruit_pur_non_detecte(self):
        # Le contre-test, indispensable : un estimateur qui trouve toujours
        # quelque chose ne prouve rien.
        res = estimate(panel_bruit(), "x", "y", "groupe", **RAPIDE)
        assert not res.excludes_zero
        assert res.p_value > 0.05

    def test_reproductible(self):
        panel = panel_simpson(n_groupes=6, par_groupe=25)
        premier = estimate(panel, "x", "y", "groupe", **RAPIDE)
        second = estimate(panel, "x", "y", "groupe", **RAPIDE)
        # Un chiffre publie ne doit pas dependre du moment ou on l'a calcule.
        assert premier == second

    def test_graine_differente_donne_un_intervalle_proche(self):
        panel = panel_simpson(n_groupes=10, par_groupe=40)
        a = estimate(panel, "x", "y", "groupe", seed=1, **RAPIDE)
        b = estimate(panel, "x", "y", "groupe", seed=2, **RAPIDE)
        # L'estimation ponctuelle ne depend pas du tirage ; les bornes, si,
        # mais faiblement. Un ecart important signalerait trop peu d'iterations.
        assert a.correlation == b.correlation
        assert abs(a.low - b.low) < 0.15

    def test_le_groupement_compte_quand_l_effet_varie_d_un_groupe_a_l_autre(self):
        """Pourquoi rechantillonner les grappes et non les lignes.

        La regle souvent repetee - "le bootstrap par grappes elargit toujours
        l'intervalle" - est FAUSSE, et ce test le montre dans les deux sens.

        Ce qui decide, c'est l'heterogeneite de l'effet entre groupes. Si tous
        les groupes racontent la meme histoire, en retirer un ne change rien
        et l'intervalle par grappes est meme plus stable que le naif. S'ils
        racontent des histoires differentes, la composition de l'echantillon
        pese lourd et l'intervalle s'elargit franchement.

        Dans les deux cas le bootstrap par grappes est le bon estimateur :
        c'est le seul qui reponde a la question "et si j'avais observe
        d'autres aeroports ?", qui est la vraie question.
        """

        def largeur_naive(panel: pd.DataFrame) -> float:
            generateur = np.random.default_rng(3)
            tirages = [
                within_correlation(
                    panel.iloc[generateur.integers(0, len(panel), len(panel))],
                    "x",
                    "y",
                    "groupe",
                )
                for _ in range(RAPIDE["iterations"])
            ]
            valides = [v for v in tirages if np.isfinite(v)]
            return float(np.quantile(valides, 0.975) - np.quantile(valides, 0.025))

        def largeur_par_grappes(panel: pd.DataFrame) -> float:
            res = estimate(panel, "x", "y", "groupe", **RAPIDE)
            return res.high - res.low

        homogene = panel_pentes([-0.9] * 10)
        heterogene = panel_pentes(list(np.linspace(-1.8, 0.2, 10)))

        assert largeur_par_grappes(heterogene) > largeur_naive(heterogene)
        assert largeur_par_grappes(homogene) < largeur_naive(homogene)

    def test_trop_peu_de_grappes(self):
        panel = panel_simpson(n_groupes=2, par_groupe=30)
        res = estimate(panel, "x", "y", "groupe", **RAPIDE)
        # On refuse de produire un intervalle sur deux grappes plutot que d'en
        # sortir un qui ne voudrait rien dire.
        assert np.isnan(res.low)
        assert np.isnan(res.p_value)

    def test_variable_constante(self):
        panel = panel_simpson(n_groupes=5, par_groupe=20).assign(y=3.0)
        res = estimate(panel, "x", "y", "groupe", **RAPIDE)
        assert np.isnan(res.correlation)


@pytest.mark.parametrize("niveau", [0.80, 0.95, 0.99])
def test_un_intervalle_plus_exigeant_est_plus_large(niveau):
    panel = panel_simpson(n_groupes=10, par_groupe=40)
    res = estimate(panel, "x", "y", "groupe", level=niveau, **RAPIDE)
    reference = estimate(panel, "x", "y", "groupe", level=0.50, **RAPIDE)
    assert (res.high - res.low) >= (reference.high - reference.low)
