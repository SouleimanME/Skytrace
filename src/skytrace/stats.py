"""Incertitude d'une correlation intra-groupe, sur donnees groupees.

Le projet publie un resultat : la correlation entre trafic aerien et NO2 au
sol, positive en brut, negative une fois la moyenne de chaque aeroport
retiree. Jusqu'ici il etait publie NU - un nombre, sans intervalle et sans
test. C'est la premiere chose qu'on demande a un resultat : est-il
distinguable du hasard ?

Deux precautions imposees par la structure de la donnee.

**Les observations ne sont pas independantes.** Le panel compte quelques
centaines de lignes, mais seulement quatorze aeroports observes sur plusieurs
dizaines d'heures chacun. Deux heures consecutives au meme aeroport se
ressemblent : meme meteo, meme trafic de fond, meme circulation autour. Un
intervalle de confiance calcule sur les lignes traiterait ces heures comme
autant de temoignages independants et sortirait un intervalle bien trop
etroit. On reechantillonne donc les GROUPES - les aeroports - et non les
lignes : c'est le bootstrap par grappes.

**Le nombre de grappes est petit.** Quatorze, c'est peu : l'intervalle
obtenu est lui-meme incertain. Ce n'est pas une raison de ne pas le calculer,
c'en est une de le dire.

Pour le test, on permute le polluant A L'INTERIEUR de chaque aeroport. Cela
conserve la structure - chaque aeroport garde ses heures et ses niveaux - et
ne detruit que l'appariement heure par heure, c'est-a-dire exactement ce que
l'hypothese nulle affirme inexistant.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

#: Assez d'iterations pour que le troisieme chiffre ne bouge plus d'un tirage
#: a l'autre, assez peu pour rester instantane sur un panel de cette taille.
DEFAULT_ITERATIONS = 2000


@dataclass(frozen=True)
class Estimate:
    """Une correlation, son intervalle et sa p-valeur."""

    correlation: float
    low: float
    high: float
    p_value: float
    clusters: int
    observations: int

    @property
    def excludes_zero(self) -> bool:
        """L'intervalle exclut-il zero ?"""
        return (self.low > 0) or (self.high < 0)


def _demean(values: np.ndarray, codes: np.ndarray, n_groups: int) -> np.ndarray:
    """Retire la moyenne de chaque groupe. C'est l'estimateur intra."""
    sommes = np.bincount(codes, weights=values, minlength=n_groups)
    effectifs = np.bincount(codes, minlength=n_groups)
    moyennes = np.divide(
        sommes, effectifs, out=np.zeros_like(sommes, dtype=float), where=effectifs > 0
    )
    return values - moyennes[codes]


def _correlation(x: np.ndarray, y: np.ndarray) -> float:
    """Pearson, sans dependance a scipy, et sans exploser sur un cas degenere."""
    if len(x) < 3:
        return float("nan")
    ecart_x, ecart_y = x.std(), y.std()
    if ecart_x == 0 or ecart_y == 0:
        return float("nan")
    return float(np.mean((x - x.mean()) * (y - y.mean())) / (ecart_x * ecart_y))


def within_correlation(frame: pd.DataFrame, x: str, y: str, cluster: str) -> float:
    """Correlation entre x et y, une fois la moyenne de chaque groupe retiree.

    C'est l'estimateur a effets fixes : il ne compare plus les aeroports entre
    eux, seulement les heures d'un meme aeroport. Tout ce qui est constant sur
    un aeroport - sa taille, sa ville, son fond de pollution - disparait.
    """
    propre = frame[[x, y, cluster]].dropna()
    if propre.empty:
        return float("nan")
    codes, uniques = pd.factorize(propre[cluster])
    return _correlation(
        _demean(propre[x].to_numpy(float), codes, len(uniques)),
        _demean(propre[y].to_numpy(float), codes, len(uniques)),
    )


def estimate(
    frame: pd.DataFrame,
    x: str,
    y: str,
    cluster: str,
    *,
    iterations: int = DEFAULT_ITERATIONS,
    level: float = 0.95,
    seed: int = 20260824,
) -> Estimate:
    """Correlation intra-groupe, intervalle par grappes et p-valeur.

    L'intervalle vient d'un bootstrap ou l'on tire des AEROPORTS avec remise,
    pas des lignes. La p-valeur vient d'une permutation du polluant a
    l'interieur de chaque aeroport.

    La graine est fixee : deux executions sur la meme donnee doivent donner le
    meme intervalle, sinon le chiffre publie depend du moment ou on l'a
    calcule.
    """
    propre = frame[[x, y, cluster]].dropna()
    groupes = [g for _, g in propre.groupby(cluster, sort=True)]
    observe = within_correlation(propre, x, y, cluster)

    if len(groupes) < 3 or not np.isfinite(observe):
        return Estimate(
            observe, float("nan"), float("nan"), float("nan"), len(groupes), len(propre)
        )

    generateur = np.random.default_rng(seed)

    # -- intervalle : on retire des aeroports, pas des heures ---------------
    tirages = []
    for _ in range(iterations):
        choisis = generateur.integers(0, len(groupes), len(groupes))
        # `keys` renumerote les grappes : un aeroport tire deux fois compte
        # comme deux grappes, ce qui est le comportement voulu.
        echantillon = pd.concat([groupes[i] for i in choisis], keys=range(len(choisis)))
        echantillon = echantillon.reset_index(level=0, names="_grappe")
        valeur = within_correlation(echantillon, x, y, "_grappe")
        if np.isfinite(valeur):
            tirages.append(valeur)

    marge = (1 - level) / 2
    low, high = (
        (float(np.quantile(tirages, marge)), float(np.quantile(tirages, 1 - marge)))
        if tirages
        else (float("nan"), float("nan"))
    )

    # -- test : on brouille l'appariement, on garde la structure ------------
    codes, uniques = pd.factorize(propre[cluster])
    valeurs_x = propre[x].to_numpy(float)
    valeurs_y = propre[y].to_numpy(float)
    x_centre = _demean(valeurs_x, codes, len(uniques))
    index_par_groupe = [np.flatnonzero(codes == g) for g in range(len(uniques))]

    au_moins_aussi_extreme = 0
    for _ in range(iterations):
        melange = valeurs_y.copy()
        for positions in index_par_groupe:
            melange[positions] = generateur.permutation(melange[positions])
        nul = _correlation(x_centre, _demean(melange, codes, len(uniques)))
        if np.isfinite(nul) and abs(nul) >= abs(observe):
            au_moins_aussi_extreme += 1

    # Correction de continuite : une p-valeur issue d'un tirage fini ne vaut
    # jamais exactement zero, et l'ecrire ainsi surinterpreterait le resultat.
    p_value = (au_moins_aussi_extreme + 1) / (iterations + 1)

    return Estimate(observe, low, high, p_value, len(uniques), len(propre))
