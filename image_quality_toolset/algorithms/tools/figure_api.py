"""pyplot-like facade over a standalone Figure.

pyplot keeps global state (current figure) and creates GUI managers, which is unsafe when
algorithms run in concurrent Processing worker threads. This keeps the plt.* drawing code
unchanged: `plt = PltFigure(figsize=...)` then use plt.subplot/plot/title/... as before.
"""
import matplotlib
from matplotlib.figure import Figure

_FIGURE_METHODS = {"suptitle", "subplots_adjust", "tight_layout"}


class PltFigure:
    def __init__(self, **kwargs):
        self.fig = Figure(**kwargs)
        self._ax = None

    rcParams = matplotlib.rcParams

    def subplot(self, *args, **kwargs):
        self._ax = self.fig.add_subplot(*args, **kwargs)
        return self._ax

    def gca(self):
        if self._ax is None:
            self._ax = self.fig.add_subplot(1, 1, 1)
        return self._ax

    def colorbar(self, *args, **kwargs):
        ax = self.gca()
        return self.fig.colorbar(ax.images[-1], ax=ax, *args, **kwargs)

    def __getattr__(self, name):
        if name in _FIGURE_METHODS:
            return getattr(self.fig, name)
        # pyplot's title/xlabel/ylabel/xlim/ylim are set_* on Axes
        ax = self.gca()
        if name in ("title", "xlabel", "ylabel", "xlim", "ylim"):
            return getattr(ax, "set_" + name)
        return getattr(ax, name)
