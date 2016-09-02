"""Module holding the parameterization scripts for the weather generator"""
import os
import os.path as osp
import tempfile
import datetime as dt
import textwrap
import six
from functools import partial
from collections import namedtuple
import inspect
import subprocess as spr
from itertools import product
import pandas as pd
import numpy as np
import calendar
import gwgen.utils as utils
from gwgen.utils import docstrings
from psyplot.compat.pycompat import OrderedDict, filterfalse


def _requirement_property(requirement):

    def get_x(self):
        return self._requirements[requirement]

    return property(
        get_x, doc=requirement + " parameterization instance")


class Parameterizer(utils.TaskBase):
    """Base class for parameterization tasks"""

    #: The registered parameterization classes (are set up automatically when
    #: subclassing this class)
    _registry = []

    #: A mapping from the keys in the namelist that are modified by this
    #: parameterization task to the key as it is used in the task information
    namelist_keys = {}

    #: A mapping from the keys in the namelist that are modified by this
    #: parameterization task to the error as stored in the configuration
    error_keys = {}

    #: A mapping from the keys in the namelist that are modified by this
    #: parameterization task to the key as it is used in the task_config
    #: attribute
    task_config_keys = {}

    @property
    def task_data_dir(self):
        """The directory where to store data"""
        return self.param_dir

    # reimplement make_run_config because of additional full_nml parameter
    @docstrings.get_sectionsf('Parameterizer.make_run_config')
    def make_run_config(self, sp, info, full_nml):
        """
        Configure the experiment

        Parameters
        ----------
        %(TaskBase.make_run_config.parameters)s
        full_nml: dict
            The dictionary with all the namelists"""
        nml = full_nml.setdefault('weathergen_ctl', OrderedDict())
        for key in ['rsquared', 'slope', 'intercept']:
            info[key] = float(sp.plotters[0].plot_data[1].attrs[key])
        nml['g_scale_coeff'] = float(
            sp.plotters[0].plot_data[1].attrs['slope'])
        nml.update(self._gp_nml)
        info.update(self._gp_info)

    @classmethod
    def get_task_for_nml_key(cls, nml_key):
        try:
            return next(para_cls for para_cls in cls._registry[::-1]
                        if nml_key in para_cls.namelist_keys)
        except StopIteration:
            raise KeyError("Unknown namelist key %s" % (nml_key, ))

    def get_error(self, nml_key):
        key, d = utils.go_through_dict(
            self.error_keys[nml_key],
            self.config['parameterization'][self.name])
        return d[key]

    @classmethod
    def get_config_key(cls, nml_key):
        return cls.task_config_keys.get(nml_key)


class DailyGHCNData(Parameterizer):
    """The parameterizer that reads in the daily data"""

    name = 'day'

    _datafile = "ghcn_daily.csv"

    dbname = 'ghcn_daily'

    summary = 'Read in the daily GHCN data'

    http_source = (
        'ftp://ftp.ncdc.noaa.gov/pub/data/ghcn/daily/ghcnd_all.tar.gz')

    http_single = (
        'ftp://ftp.ncdc.noaa.gov/pub/data/ghcn/daily/all/{}')

    @property
    def default_config(self):
        return default_daily_ghcn_config()._replace(
            **super(DailyGHCNData, self).default_config._asdict())

    @property
    def data_dir(self):
        return osp.join(super(DailyGHCNData, self).data_dir,
                        'ghcn', 'ghcnd_all')

    @property
    def raw_src_files(self):
        return list(map(lambda s: osp.join(self.data_dir, s + '.dly'),
                        self.stations))

    flags = ['tmax_m', 'prcp_s', 'tmax_q', 'prcp_m', 'tmin_m', 'tmax_s',
             'tmin_s', 'prcp_q', 'tmin_q']

    @property
    def sql_dtypes(self):
        import sqlalchemy
        ret = super(DailyGHCNData, self).sql_dtypes
        flags = self.flags
        ret.update({flag: sqlalchemy.CHAR(length=1) for flag in flags})
        return ret

    def setup_from_file(self, *args, **kwargs):
        kwargs['index_col'] = ['id', 'year', 'month', 'day']
        kwargs['dtype'] = {
            'prcp': np.float64,
            'prcp_m': str,
            'prcp_q': str,
            'prcp_s': str,
            'tmax': np.float64,
            'tmax_m': str,
            'tmax_q': str,
            'tmax_s': str,
            'tmin': np.float64,
            'tmin_m': str,
            'tmin_q': str,
            'tmin_s': str}
        super(DailyGHCNData, self).setup_from_file(*args, **kwargs)
        for flag in self.flags:
            self.data[flag].fillna('', inplace=True)

    def setup_from_db(self, *args, **kwargs):
        kwargs['index_col'] = ['id', 'year', 'month', 'day']
        super(DailyGHCNData, self).setup_from_db(*args, **kwargs)
        for flag in self.flags:
            self.data[flag] = self.data[flag].str.strip()

    def setup_from_scratch(self):
        from gwgen.parseghcnrow import read_ghcn_file
        logger = self.logger
        stations = self.stations
        logger.debug('Reading daily ghcn data for %s stations', len(stations))
        src_dir = self.data_dir
        logger.debug('    Data source: %s', src_dir)
        files = list(map(lambda s: osp.join(src_dir, s + '.dly'), stations))
        self.data = pd.concat(
            list(map(read_ghcn_file, files)), copy=False).set_index(
                ['id', 'year', 'month', 'day'])
        self.logger.debug('Done.')

    @classmethod
    def _modify_parser(cls, parser):
        parser.setup_args(default_daily_ghcn_config)
        parser, setup_grp, run_grp = super(DailyGHCNData, cls)._modify_parser(
            parser)
        parser.update_arg('download', short='d', group=setup_grp,
                          choices=['single', 'all'])
        return parser, setup_grp, run_grp

    def init_from_scratch(self):
        """Reimplemented to download the data if not existent"""
        logger = self.logger
        logger.debug('Initializing %s', self.name)
        stations = self.stations
        logger.debug('Reading data for %s stations', len(stations))
        src_dir = self.data_dir
        logger.debug('    Expected data source: %s', src_dir)
        files = self.raw_src_files
        missing = list(filterfalse(osp.exists, files))
        if missing:
            download = self.task_config.download
            msg = '%i Required files are not existent in %s' % (
                len(missing), src_dir)
            if download == 'single':
                logger.debug(msg)
                for f in missing:
                    utils.download_file(
                        self.http_single.format(osp.basename(f)), f)
            else:
                import tarfile
                logger.debug(msg)
                tarfname = self.model_config.get('ghcn_src', src_dir +
                                                 '.tar.gz')
                if not osp.exists(tarfname):
                    if download is None:
                        if six.PY2:
                            raise IOError(msg)
                        else:
                            raise FileNotFoundError(msg)
                    else:
                        logger.debug('    Downloading rawdata from %s',
                                     self.http_source)
                        if not osp.exists(osp.dirname(tarfname)):
                            os.makedirs(osp.dirname(tarfname))
                        utils.download_file(self.http_source, tarfname)
                        self.model_config['ghcn_download'] = dt.datetime.now()
                    self.model_config['ghcn_src'] = tarfname
                taro = tarfile.open(tarfname, 'r|gz')
                logger.debug('    Extracting to %s', osp.dirname(src_dir))
                taro.extractall(osp.dirname(src_dir))


_DailyGHCNConfig = namedtuple('_DailyGHCNConfig', ['download'])

_DailyGHCNConfig = utils.append_doc(
    _DailyGHCNConfig, docstrings.get_sections("""
Parameters
----------
download: { 'single' | 'all' | None }
    What to do if a stations file is missing. The default is ``None`` which
    raises an Error.
    Otherwise, if ``'single'``, download the missing file from %s. If ``'all'``
    the entire tarball is downloaded from %s
""" % (DailyGHCNData.http_single, DailyGHCNData.http_source),
        '_DailyGHCNConfig'))


DailyGHCNConfig = utils.enhanced_config(_DailyGHCNConfig, 'DailyGHCNConfig')


@docstrings.dedent
def default_daily_ghcn_config(
        download=None, *args, **kwargs):
    """
    The default configuration for :class:`DailyGHCNData` instances.
    See also the :attr:`DailyGHCNData.default_config` attribute

    Parameters
    ----------
    %(DailyGHCNConfig.parameters)s"""
    return DailyGHCNConfig(download, *utils.default_config(*args, **kwargs))


class MonthlyGHCNData(Parameterizer):
    """The parameterizer that calculates the monthly summaries from the daily
    data"""

    name = 'month'

    setup_requires = ['day']

    _datafile = "ghcn_monthly.csv"

    dbname = 'ghcn_monthly'

    summary = "Calculate monthly means from the daily GHCN data"

    @property
    def sql_dtypes(self):
        import sqlalchemy
        ret = super(MonthlyGHCNData, self).sql_dtypes
        for col in set(self.data.columns).difference(ret):
            if 'complete' in col:
                ret[col] = sqlalchemy.BOOLEAN
            else:
                ret[col] = sqlalchemy.REAL
        return ret

    def setup_from_file(self, *args, **kwargs):
        kwargs['index_col'] = ['id', 'year', 'month']
        return super(MonthlyGHCNData, self).setup_from_file(*args, **kwargs)

    def setup_from_db(self, *args, **kwargs):
        kwargs['index_col'] = ['id', 'year', 'month']
        return super(MonthlyGHCNData, self).setup_from_db(*args, **kwargs)

    @staticmethod
    def monthly_summary(df):
        n = calendar.monthrange(df.index[0][1], df.index[0][2])[1]
        return pd.DataFrame.from_dict(OrderedDict([
            ('tmin', [df.tmin.mean()]), ('tmax', [df.tmax.mean()]),
            ('trange', [(df.tmax - df.tmin).mean()]),
            ('prcp', [df.prcp.sum()]), ('tmin_abs', [df.tmin.min()]),
            ('tmax_abs', [df.tmax.max()]), ('prcpmax', [df.prcp.max()]),
            ('tmin_complete', [df.tmin.count() == n]),
            ('tmax_complete', [df.tmax.count() == n]),
            ('prcp_complete', [df.prcp.count() == n]),
            ('wet_day', [df.prcp[df.prcp.notnull() & (df.prcp > 0)].size])]))

    def setup_from_scratch(self):
        def year_complete(series):
            """Check whether the data for the given is complete"""
            return series.astype(int).sum() == 12
        data = self.day.data.groupby(level=['id', 'year', 'month']).apply(
            self.monthly_summary)
        data.index = data.index.droplevel(-1)

        complete_cols = [col for col in data.columns
                         if col.endswith('complete')]

        df_yearly = data[complete_cols].groupby(level=['id', 'year']).agg(
            year_complete)

        names = data.index.names
        data = data.reset_index().merge(
            df_yearly[complete_cols].reset_index(), on=['id', 'year'],
            suffixes=['', '_year']).set_index(names)

        self.data = data


class CompleteMonthlyGHCNData(MonthlyGHCNData):
    """The parameterizer that calculates the monthly summaries from the daily
    data"""

    name = 'cmonth'

    setup_requires = ['month']

    _datafile = "complete_ghcn_monthly.csv"

    dbname = 'complete_ghcn_monthly'

    summary = "Extract the complete months from the monthly data"

    def setup_from_scratch(self):
        all_months = self.month.data
        self.data = all_months[all_months.prcp_complete &
                               all_months.tmin_complete &
                               all_months.tmax_complete]


class YearlyCompleteMonthlyGHCNData(CompleteMonthlyGHCNData):
    """The parameterizer that calculates the monthly summaries from the daily
    data"""

    name = 'yearly_cmonth'

    setup_requires = ['month']

    _datafile = "yearly_complete_ghcn_monthly.csv"

    dbname = 'yearly_complete_ghcn_monthly'

    summary = (
        "Extract the complete months from the monthly data in complete years")

    def setup_from_scratch(self):
        all_months = self.month.data
        self.data = all_months[all_months.prcp_complete_year &
                               all_months.tmin_complete_year &
                               all_months.tmax_complete_year]


class CompleteDailyGHCNData(DailyGHCNData):
    """The parameterizer that calculates the days in complete months"""

    name = 'cday'

    setup_requires = ['day', 'month']

    _datafile = "complete_ghcn_daily.csv"

    dbname = 'complete_ghcn_daily'

    summary = "Get the days of the complete months"

    @property
    def default_config(self):
        return super(DailyGHCNData, self).default_config

    @classmethod
    def _modify_parser(cls, parser):
        return super(DailyGHCNData, cls)._modify_parser(parser)

    def init_from_scratch(self):
        pass

    def setup_from_scratch(self):
        monthly = self.month.data
        self.data = self.day.data.reset_index().merge(
            monthly[monthly.prcp_complete &
                    monthly.tmin_complete &
                    monthly.tmax_complete][[]].reset_index(),
            how='inner', on=['id', 'year', 'month'], copy=False).set_index(
                ['id', 'year', 'month', 'day'])


class YearlyCompleteDailyGHCNData(CompleteDailyGHCNData):
    """The parameterizer that calculates the days in complete months"""

    name = 'yearly_cday'

    setup_requires = ['day', 'month']

    _datafile = "yearly_complete_ghcn_daily.csv"

    dbname = 'yearly_complete_ghcn_daily'

    summary = "Get the days of the complete months in complete years"

    def setup_from_scratch(self):
        monthly = self.month.data
        self.data = self.day.data.reset_index().merge(
            monthly[monthly.prcp_complete_year &
                    monthly.tmin_complete_year &
                    monthly.tmax_complete_year][[]].reset_index(),
            how='inner', on=['id', 'year', 'month'], copy=False).set_index(
                ['id', 'year', 'month', 'day'])

_PrcpConfig = namedtuple('_PrcpConfig', ['thresh', 'threshs2compute'])

_PrcpConfig = utils.append_doc(_PrcpConfig, docstrings.get_sections("""
Parameters
----------
thresh: float
    The threshold to use for the configuration
threshs2compute: list of floats
    The thresholds to compute during the setup of the data
""", '_PrcpConfig'))


PrcpConfig = utils.enhanced_config(_PrcpConfig, 'PrcpConfig')


@docstrings.dedent
def default_prcp_config(
        thresh=15., threshs2compute=[5, 7.5, 10, 12.5, 15, 17.5, 20],
        *args, **kwargs):
    """
    The default configuration for :class:`PrcpDistParams` instances.
    See also the :attr:`PrcpDistParams.default_config` attribute

    Parameters
    ----------
    %(PrcpConfig.parameters)s"""
    return PrcpConfig(thresh, threshs2compute,
                      *utils.default_config(*args, **kwargs))


class PrcpDistParams(Parameterizer):
    """The parameterizer to calculate the precipitation distribution parameters
    """

    name = 'prcp'

    setup_requires = ['cday']

    _datafile = "prcp_dist_parameters.csv"

    dbname = 'prcp_dist_params'

    summary = ('Calculate the precipitation distribution parameters of the '
               'hybrid Gamma-GP')

    namelist_keys = {'thresh': 'thresh', 'g_scale_coeff': 'slope',
                     'gp_shape': 'gpshape'}

    error_keys = {'gp_shape': 'gpshape_std'}

    task_config_keys = {'thresh': 'thresh'}

    has_run = True

    #: default formatoptions for the
    #: :class:`psyplot.plotter.linreg.DensityRegPlotter` plotter
    fmt = kwargs = dict(
        legend={'loc': 'upper left'},
        cmap='w_Reds',
        precision=0.1,
        xlabel='{desc}',
        ylabel='{desc}',
        xrange=(0, ['rounded', 95]),
        yrange=(0, ['rounded', 95]),
        fix=0,
        legendlabels=['$\\theta$ = %(slope)1.4f * $\\bar{{p}}_d$'],
        bounds=['minmax', 11, 0, 99],
        cbar='',
        bins=10,
        )

    @property
    def default_config(self):
        return default_prcp_config()._replace(
            **super(PrcpDistParams, self).default_config._asdict())

    @property
    def sql_dtypes(self):
        import sqlalchemy
        ret = super(PrcpDistParams, self).sql_dtypes
        for flag in {'gshape', 'pscale', 'gscale', 'pscale_orig', 'mean_wet',
                     'thresh', 'pshape'}:
            ret[flag] = sqlalchemy.REAL
        for flag in {'n', 'ngamma', 'ngp'}:
            ret[flag] = sqlalchemy.BIGINT
        return ret

    @classmethod
    def _modify_parser(cls, parser):
        parser.setup_args(default_prcp_config)
        parser, setup_grp, run_grp = super(PrcpDistParams, cls)._modify_parser(
            parser)
        parser.update_arg('thresh', short='t', group=run_grp)
        parser.append2help('thresh', '. Default: %(default)s')
        parser.update_arg('threshs2compute', short='t2c', group=setup_grp,
                          type=float, nargs='+', metavar='float')
        return parser, setup_grp, run_grp

    def setup_from_file(self, *args, **kwargs):
        kwargs['index_col'] = ['id', 'month', 'thresh']
        return super(PrcpDistParams, self).setup_from_file(*args, **kwargs)

    def setup_from_db(self, *args, **kwargs):
        kwargs['index_col'] = ['id', 'month', 'thresh']
        return super(PrcpDistParams, self).setup_from_db(*args, **kwargs)

    @property
    def filtered_data(self):
        """Return the data that only belongs to the specified threshold of the
        task"""
        sl = (slice(None), slice(None), self.task_config.thresh)
        return self.data.sort_index().loc[sl, :]

    def prcp_dist_params(
            self, df, threshs=np.array([5, 7.5, 10, 12.5, 15, 17.5, 20])):
        from scipy import stats
        vals = df.prcp.values[~np.isnan(df.prcp.values)]
        N = len(threshs)
        n = len(vals) * N
        vals = vals[vals > 0]
        ngamma = len(vals)
        ngp = [np.nan] * N
        gshape = np.nan
        gscale = np.nan
        pshape = [np.nan] * N
        pscale = [np.nan] * N
        pscale_orig = [np.nan] * N
        if ngamma > 10:
            # fit the gamma curve. We fix the (unnecessary) location parameter
            # to improve the result (see
            # http://stackoverflow.com/questions/16963415/why-does-the-gamma-distribution-in-scipy-have-three-parameters)
            try:
                gshape, _, gscale = stats.gamma.fit(vals, floc=0)
            except:
                self.logger.critical(
                    'Error while calculating GP parameters for %s!',
                    ', '.join(str(df.index.get_level_values(n).unique())
                              for n in df.index.names),
                    exc_info=True)
                tmp = tempfile.NamedTemporaryFile(suffix='.csv').name
                df.to_csv(tmp)
                self.logger.critical('Data stored in %s', tmp)
            else:
                for i, thresh in enumerate(threshs):
                    arr = vals[vals >= thresh]
                    ngp[i] = len(arr)
                    if ngp[i] > 10:
                        try:
                            pshape[i], _, pscale_orig[i] = stats.genpareto.fit(
                                arr, floc=thresh)
                        except:
                            self.logger.critical(
                                'Error while calculating GP parameters for '
                                '%s!', ', '.join(
                                    str(df.index.get_level_values(n).unique())
                                    for n in df.index.names),
                                exc_info=True)
                            tmp = tempfile.NamedTemporaryFile(
                                suffix='.csv').name
                            df.to_csv(tmp)
                            self.logger.critical('Data stored in %s', tmp)
                        else:
                            # find the crossover point where the gamma and
                            # pareto distributions should match this follows
                            # Neykov et al. (Nat. Hazards Earth Syst. Sci.,
                            # 14, 2321-2335, 2014) bottom of page 2330 (left
                            # column)
                            pscale[i] = (1 - stats.gamma.cdf(
                                thresh, gshape, scale=gscale))/stats.gamma.pdf(
                                    thresh, gshape, scale=gscale)
        return pd.DataFrame.from_dict(OrderedDict([
            ('n', np.repeat(n, N)), ('ngamma', np.repeat(ngamma, N)),
            ('mean_wet', np.repeat(vals.mean(), N)),
            ('ngp', ngp), ('thresh', threshs),
            ('gshape', np.repeat(gshape, N)),
            ('gscale', np.repeat(gscale, N)), ('pshape', pshape),
            ('pscale', pscale),
            ('pscale_orig', pscale_orig)])).set_index('thresh')

    def setup_from_scratch(self):
        self.logger.debug('Calculating precipitation parameters.')
        df = self.cday.data
        threshs = np.array(self.task_config.threshs2compute)
        if self.task_config.thresh not in threshs:
            threshs = np.append(threshs, self.task_config.thresh)
        func = partial(self.prcp_dist_params, threshs=threshs)
        self.data = df.groupby(level=['id', 'month']).apply(func)
        self.logger.debug('Done.')

    @property
    def ds(self):
        """The dataset of the :attr:`data` dataframe"""
        import xarray as xr
        data = self.filtered_data.set_index('mean_wet')
        ds = xr.Dataset.from_dataframe(data)
        ds.mean_wet.attrs['long_name'] = 'Mean precip. on wet days'
        ds.mean_wet.attrs['units'] = 'mm'
        ds.gscale.attrs['long_name'] = 'Gamma scale parameter'
        ds.gscale.attrs['units'] = 'mm'
        return ds

    def create_project(self, ds):
        """Make the gamma shape - number of wet days plot

        Parameters
        ----------
        %(TaskBase.create_project.parameters)s
        """
        import seaborn as sns
        import psyplot.project as psy
        sns.set_style("white")
        return psy.plot.densityreg(ds, name='gscale', fmt=self.fmt)

    def make_run_config(self, sp, info, full_nml):
        """
        Configure the experiment with information on gamma scale and GP shape

        Parameters
        ----------
        %(Parameterizer.make_run_config.parameters)s"""
        nml = full_nml.setdefault('weathergen_ctl', OrderedDict())
        for key in ['rsquared', 'slope', 'intercept']:
            info[key] = float(sp.plotters[0].plot_data[1].attrs[key])
        nml['g_scale_coeff'] = float(
            sp.plotters[0].plot_data[1].attrs['slope'])
        nml['thresh'] = self.task_config.thresh
        nml.update(self._gp_nml)
        info.update(self._gp_info)

    @docstrings.dedent
    def plot_additionals(self, pdf=None):
        """
        Plot the histogram of GP shape

        Parameters
        ----------
        %(TaskBase.plot_additionals.parameters)s
        """
        import seaborn as sns
        import matplotlib.pyplot as plt
        sns.set_style('darkgrid')

        df = self.filtered_data

        fig = plt.figure(figsize=(12, 2.5))
        fig.subplots_adjust(hspace=0)
        ax2 = plt.subplot2grid((4, 2), (0, 1))
        ax3 = plt.subplot2grid((4, 2), (1, 1), rowspan=3, sharex=ax2)
        pshape = df.pshape[df.ngp > 100]
        sns.boxplot(pshape, ax=ax2, whis=[1, 99], showmeans=True,
                    meanline=True)
        sns.distplot(pshape, hist=True, kde=True, ax=ax3)
        ax3.set_xlim(*np.percentile(pshape, [0.1, 99.9]))
        ax3.set_xlabel('Generalized Pareto Shape parameter')
        ax3.set_ylabel('Counts')

        median = float(np.round(pshape.median(), 4))
        mean = float(np.round(pshape.mean(), 4))
        std = float(np.round(pshape.std(), 4))

        median_line = next(
            l for l in ax2.lines if np.all(
                np.round(l.get_xdata(), 4) == median))
        mean_line = next(
            l for l in ax2.lines if np.all(np.round(l.get_xdata(), 4) == mean))
        ax3.legend(
            (median_line, mean_line),
            ('median = %1.4f' % median, 'mean = %1.4f' % mean), loc='center',
            bbox_to_anchor=[0.7, 0.2], bbox_transform=ax3.transAxes)
        if pdf:
            pdf.savefig(fig, bbox_inches='tight')
        else:
            plt.savefig(self.pdf_file, bbox_inches='tight')

        self._gp_nml = dict(gp_shape=mean)

        self._gp_info = dict(gpshape_mean=mean, gpshape_median=median,
                             gpshape_std=std)


class MarkovChain(Parameterizer):
    """The parameterizer to calculate the Markov Chain parameters"""

    name = 'markov'

    setup_requires = ['cday']

    _datafile = 'markov_chain_parameters.csv'

    dbname = 'markov'

    summary = "Calculate the markov chain parameterization"

    has_run = True

    namelist_keys = ['p101_1', 'p001_1', 'p001_2', 'p11_1', 'p11_2', 'p101_2']

    fmt = kwargs = dict(
        legend={'loc': 'upper left'},
        cmap='w_Reds',
        ylabel='%(long_name)s',
        xlim=(0, 1),
        ylim=(0, 1),
        fix=0,
        bins=10,
        bounds=['minmax', 11, 0, 99],
        cbar='',
        ci=None,
        legendlabels=['$%(symbol)s$ = %(slope)1.4f * %(xname)s'],
        )

    @property
    def sql_dtypes(self):
        import sqlalchemy
        ret = super(MarkovChain, self).sql_dtypes
        for flag in {'p11', 'p001', 'wetf', 'p101', 'p01'}:
            ret[flag] = sqlalchemy.REAL
        return ret

    def setup_from_file(self, *args, **kwargs):
        kwargs['index_col'] = ['id', 'month']
        return super(MarkovChain, self).setup_from_file(*args, **kwargs)

    def setup_from_db(self, *args, **kwargs):
        kwargs['index_col'] = ['id', 'month']
        return super(MarkovChain, self).setup_from_db(*args, **kwargs)

    @classmethod
    def calc_ndays(cls, df):
        if not len(df):
            return pd.DataFrame([[np.nan] * 9], columns=[
                'n', 'nwet', 'ndry', 'np11', 'np01',  'np001', 'np001_denom',
                'np101', 'np101_denom'], dtype=int)
        vals = df.prcp.values
        n = len(vals)
        nwet = (vals[:-1] > 0.0).sum()   #: number of wet days
        ndry = (vals[:-1] == 0.0).sum()  #: number of dry days
        # ---------------------------------------------------
        # PWW = Prob(Wet then Wet) = P11
        #     = wetwet / wetwet + wetdry
        #     = wetwet / nwet
        np11 = ((vals[:-1] > 0.0) & (vals[1:] > 0.0)).sum()
        # ---------------------------------------------------
        # PWD = Prob(Dry then Wet) = P01
        #     = drywet / drywet + drydry
        #     = wetwet / ndry
        np01 = ((vals[:-1] == 0.0) & (vals[1:] > 0.0)).sum()
        # ---------------------------------------------------
        # PWDD = Prob(Dry Dry Wet) = P001
        #      = drydryWET / (drydryWET + drydryDRY)
        np001 = ((vals[:-2] == 0.0) & (vals[1:-1] == 0.0) &
                 (vals[2:] > 0.0)).sum()
        np001_denom = np001 + ((vals[:-2] == 0.0) & (vals[1:-1] == 0.0) &
                               (vals[2:] == 0.0)).sum()
        # ---------------------------------------------------
        # PWDW = Prob(Wet Dry Wet) = P101
        #      = wetdryWET / (wetdryWET + wetdryDRY)
        np101 = ((vals[:-2] > 0.0) & (vals[1:-1] == 0.0) &
                 (vals[2:] > 0.0)).sum()
        np101_denom = np101 + ((vals[:-2] > 0.0) & (vals[1:-1] == 0.0) &
                               (vals[2:] == 0.0)).sum()
        return pd.DataFrame(
            [[n, nwet, ndry, np11, np01, np001, np001_denom, np101,
              np101_denom]],
            columns=['n', 'nwet', 'ndry', 'np11', 'np01', 'np001',
                     'np001_denom', 'np101', 'np101_denom'])

    @classmethod
    def calculate_probabilities(cls, df):
        """Calculate the transition probabilities for one month across multiple
        years"""
        # we group here for each month because we do not want to treat each
        # month separately
        g = df.groupby(level=['year', 'month'])
        if g.ngroups > 10:
            dfs = g.apply(cls.calc_ndays).sum()
            return pd.DataFrame.from_dict(OrderedDict([
                ('p11', [dfs.np11 / dfs.nwet if dfs.nwet > 0 else 0]),
                ('p01', [dfs.np01 / dfs.ndry if dfs.ndry > 0 else 0]),
                ('p001', [dfs.np001 / dfs.np001_denom
                          if dfs.np001_denom > 0 else 0]),
                ('p101', [dfs.np101 / dfs.np101_denom
                          if dfs.np101_denom > 0 else 0]),
                ('wetf', [dfs.nwet / dfs.n if dfs.n > 0 else 0])]))
        else:
            return pd.DataFrame.from_dict(
                {'p11': [], 'p01': [], 'p001': [], 'p101': [], 'wetf': []})

    def setup_from_scratch(self):
        self.logger.debug('Calculating markov chain parameters')
        df = self.cday.data
        data = df.groupby(level=['id', 'month']).apply(
            self.calculate_probabilities)
        data.index = data.index.droplevel(-1)
        self.data = data
        self.logger.debug('Done.')

    @property
    def ds(self):
        """The dataset of the :attr:`data` DataFrame"""
        import xarray as xr
        ds = xr.Dataset.from_dataframe(self.data.set_index('wetf'))
        ds.wetf.attrs['long_name'] = 'Fraction of wet days'
        ds.p11.attrs['long_name'] = 'Prob. Wet then Wet'
        ds.p101.attrs['long_name'] = 'Prob. Wet then Dry then Wet'
        ds.p001.attrs['long_name'] = 'Prob. Dry then Dry then Wet'
        ds.p11.attrs['symbol'] = 'p_{11}'
        ds.p101.attrs['symbol'] = 'p_{101}'
        ds.p001.attrs['symbol'] = 'p_{001}'
        return ds

    @docstrings.dedent
    def create_project(self, ds):
        """
        Create the project of the plots of the transition probabilities

        Parameters
        ----------
        %(TaskBase.create_project.parameters)s"""
        import matplotlib.pyplot as plt
        import seaborn as sns
        import psyplot.project as psy
        sns.set_style('white')
        fig, axes = plt.subplots(3, 1, figsize=(10, 12))
        axes = axes.ravel()
        sp = psy.plot.densityreg(
            ds, name=['p11', 'p101', 'p001'], fmt=self.fmt, ax=axes.ravel(),
            share='xlim')
        sp(name='p11').update(
            fix=[(1., 1.)], legendlabels=[
                '$%(symbol)s$ = %(intercept)1.4f + '
                '%(slope)1.4f * %(xname)s'])
        sp(ax=axes[-1]).update(xlabel='%(long_name)s')
        return sp

    @docstrings.dedent
    def make_run_config(self, sp, info, full_nml):
        """
        Configure the experiment with the MarkovChain relationships

        Parameters
        ----------
        %(Parameterizer.make_run_config.parameters)s"""
        nml = full_nml.setdefault('weathergen_ctl', OrderedDict())
        for plotter in sp.plotters:
            d = info[plotter.data.name] = OrderedDict()
            for key in ['rsquared', 'slope', 'intercept']:
                d[key] = float(plotter.plot_data[1].attrs[key])
        for plotter in sp.plotters:
            name = plotter.data.name
            nml[name + '_1'] = float(plotter.plot_data[1].attrs.get(
                'intercept', 0))
            nml[name + '_2'] = float(plotter.plot_data[1].attrs.get('slope'))


class TemperatureParameterizer(Parameterizer):
    """Parameterizer to correlate the monthly mean and standard deviation on
    wet and dry days with the montly mean"""

    name = 'temp'

    summary = 'Temperature mean correlations'

    setup_requires = ['cday']

    _datafile = 'temperature.csv'

    dbname = 'temperature'

    has_run = True

    namelist_keys = {
        'tmin_w1': 'tmin_wet.intercept',
        'tmin_w2': 'tmin_wet.slope',
        'tmin_d1': 'tmin_dry.intercept',
        'tmin_d2': 'tmin_dry.slope',
        'tmin_sd_w1': 'tminstddev_wet.intercept',
        'tmin_sd_w2': 'tminstddev_wet.slope',
        'tmin_sd_d1': 'tminstddev_dry.intercept',
        'tmin_sd_d2': 'tminstddev_dry.slope',
        'tmax_w1': 'tmax_wet.intercept',
        'tmax_w2': 'tmax_wet.slope',
        'tmax_d1': 'tmax_dry.intercept',
        'tmax_d2': 'tmax_dry.slope',
        'tmax_sd_w1': 'tmaxstddev_wet.intercept',
        'tmax_sd_w2': 'tmaxstddev_wet.slope',
        'tmax_sd_d1': 'tmaxstddev_dry.intercept',
        'tmax_sd_d2': 'tmaxstddev_dry.slope'}

    fmt = dict(
        legend={'loc': 'upper left'},
        cmap='w_Reds',
        precision=0.1,
        xrange=(0, ['rounded', 95]),
        yrange=(0, ['rounded', 95]),
        legendlabels=[
            '$%(symbol)s$ = %(intercept)1.4f + %(slope)1.4f * $%(xsymbol)s$'],
        bounds=['minmax', 11, 0, 99],
        cbar='',
        bins=10,
        xlabel='on %(state)s days'
        )

    @property
    def sql_dtypes(self):
        import sqlalchemy
        ret = super(TemperatureParameterizer, self).sql_dtypes
        for col in set(self.data.columns).difference(ret):
            ret[col] = sqlalchemy.REAL
        return ret

    @property
    def ds(self):
        """The dataframe of this parameterization task converted to a dataset
        """
        import xarray as xr
        cols = [col for col in self.data.columns if (
                   col.startswith('tmin') or col.startswith('tmax'))]
        ds = xr.Dataset.from_dataframe(self.data[cols].reset_index())
        for v, t, state in product(['tmin', 'tmax'], ['stddev', ''],
                                   ['', 'wet', 'dry']):
            vname = v + t + (('_' + state) if state else '')
            varo = ds.variables[vname]
            what = v[1:]
            label = 'std. dev.' if t else 'mean'
            varo.attrs['long_name'] = '%s of %s. temperature' % (label, what)
            varo.attrs['units'] = 'degC'
            varo.attrs['symbol'] = 't_\mathrm{{%s%s%s}}' % (
                what, ', sd' if t else '', (', ' + state) if state else '')
            varo.attrs['state'] = state or 'all'
        return ds

    @staticmethod
    def calc_monthly_props(df):
        """
        Calculate the statistics for one single month in one year
        """
        prcp_vals = df.prcp.values
        wet = prcp_vals > 0.0
        dry = prcp_vals == 0
        arr_tmin = df.tmin.values
        arr_tmax = df.tmax.values
        arr_tmin_wet = arr_tmin[wet]
        arr_tmin_dry = arr_tmin[dry]
        arr_tmax_wet = arr_tmax[wet]
        arr_tmax_dry = arr_tmax[dry]
        # prcp values
        d = {
            # wet values
            'tmin_wet': arr_tmin_wet.mean(),
            'tmax_wet': arr_tmax_wet.mean(),
            'tminstddev_wet': arr_tmin_wet.std(),
            'tmaxstddev_wet': arr_tmax_wet.std(),
            'trange_wet': (arr_tmax_wet - arr_tmax_wet).mean(),
            'trangestddev_wet': (arr_tmax_wet - arr_tmax_wet).std(),
            # dry values
            'tmin_dry': arr_tmin_dry.mean(),
            'tmax_dry': arr_tmax_dry.mean(),
            'tminstddev_dry': arr_tmin_dry.std(),
            'tmaxstddev_dry': arr_tmax_dry.std(),
            'trange_dry': (arr_tmax_dry - arr_tmax_dry).mean(),
            'trangestddev_dry': (arr_tmax_dry - arr_tmax_dry).std(),
            # general mean
            'tmin': arr_tmin.mean(),
            'tmax': arr_tmax.mean(),
            'tminstddev': arr_tmin.std(),
            'tmaxstddev': arr_tmax.std(),
            'trange': (arr_tmin - arr_tmax).mean(),
            'trangestddev': (arr_tmin - arr_tmax).std(),
            't': ((arr_tmin + arr_tmax) * 0.5).mean(),
            'tstddev': ((arr_tmin + arr_tmax) * 0.5).std()}
        d['prcp_wet'] = am = prcp_vals[wet].mean()  # arithmetic mean
        gm = np.exp(np.log(prcp_vals[wet]).mean())  # geometric mean
        fields = am != gm
        d['alpha'] = (
            0.5000876 / np.log(am[fields] / gm[fields]) + 0.16488552 -
            0.0544274 * np.log(am[fields] / gm[fields]))
        d['beta'] = am / d['alpha']
        return pd.DataFrame.from_dict(d)

    @classmethod
    def calculate_probabilities(cls, df):
        """Calculate the statistics for one month across multiple years"""
        # we group here for each month because we do not want to treat each
        # month separately
        g = df.groupby(level=['year'])
        return g.apply(cls.calc_monthly_props).mean()

    def setup_from_file(self, *args, **kwargs):
        kwargs['index_col'] = ['id', 'month']
        return super(TemperatureParameterizer, self).setup_from_file(
            *args, **kwargs)

    def setup_from_db(self, *args, **kwargs):
        kwargs['index_col'] = ['id', 'month']
        return super(TemperatureParameterizer, self).setup_from_db(
            *args, **kwargs)

    def setup_from_scratch(self):
        self.data = self.cday.data.groupby(level=['id', 'month']).apply(
            self.calculate_probabilities)

    def create_project(self, ds):
        """
        Create the plots of the wet/dry - mean relationships

        Parameters
        ----------
        %(TaskBase.create_project)s"""
        import matplotlib.pyplot as plt
        import seaborn as sns
        import psyplot.project as psy
        sns.set_style('white')
        axes = np.concatenate([
                plt.subplots(1, 2, figsize=(12, 4))[1] for _ in range(4)])
        for fig in set(ax.get_figure() for ax in axes):
            fig.subplots_adjust(bottom=0.25)
        middle = (
            axes[0].get_position().x0 + axes[1].get_position().x1) / 2.
        axes = iter(axes)
        variables = ['tmin', 'tmax']
        types = ['', 'stddev']
        for v, t in product(variables, types):
            base = '%s%s' % (v, t)
            psy.plot.densityreg(
                ds, name=base + '_wet', ax=next(axes),
                coord=v if not t else v + '_wet',
                ylabel='%(long_name)s\non %(state)s days',
                text=[(middle, 0.03, '%(long_name)s', 'fig', dict(
                     weight='bold', ha='center'))], fmt=self.fmt)
            psy.plot.densityreg(
                ds, name=base + '_dry', ax=next(axes),
                coord=v if not t else v + '_dry',
                ylabel='on %(state)s days', fmt=self.fmt)
        return psy.gcp(True)[:]

    @docstrings.dedent
    def make_run_config(self, sp, info, full_nml):
        """
        Configure the experiment with the correlations of wet/dry temperature
        to mean temperature

        Parameters
        ----------
        %(Parameterizer.make_run_config.parameters)s
        """
        variables = ['tmin', 'tmax']
        states = ['wet', 'dry']
        types = ['', 'stddev']
        nml = full_nml.setdefault('weathergen_ctl', OrderedDict())
        for v, t, state in product(variables, types, states):
            vname = '%s%s_%s' % (v, t, state)
            nml_name = v + ('_sd' if t else '') + '_' + state[0]
            vinfo = info.setdefault(vname, {})
            plotter = sp(name=vname).plotters[0]
            for key in ['rsquared', 'slope', 'intercept']:
                vinfo[key] = float(plotter.plot_data[1].attrs[key])
            nml[nml_name + '1'] = float(
                plotter.plot_data[1].attrs.get('intercept', 0))
            nml[nml_name + '2'] = float(
                plotter.plot_data[1].attrs.get('slope'))


_CloudConfig = namedtuple('_CloudConfig', ['args_type'])

_CloudConfig = utils.append_doc(_CloudConfig, docstrings.get_sections("""
Parameters
----------
args_type: str
    The type of the stations. One of

    ghcn
        Stations are GHCN ids
    eecra
        Stations are EECRA station numbers
""", '_CloudConfig'))


CloudConfig = utils.enhanced_config(_CloudConfig, 'CloudConfig')


@docstrings.dedent
def default_cloud_config(args_type='ghcn', *args, **kwargs):
    """
    The default configuration for :class:`CloudParameterizerBase` instances.
    See also the :attr:`CloudParameterizerBase.default_config` attribute

    Parameters
    ----------
    %(CloudConfig.parameters)s"""
    return CloudConfig(args_type, *utils.default_config(*args, **kwargs))


class CloudParameterizerBase(Parameterizer):
    """Abstract base class for cloud parameterizers"""

    allow_files = False

    @property
    def default_config(self):
        return default_cloud_config()._replace(
            **super(CloudParameterizerBase, self).default_config._asdict())

    @property
    def args_type(self):
        return self.task_config.args_type

    @property
    def setup_from(self):
        if self.allow_files and self.args_type == 'files':
            return 'scratch'
        return super(CloudParameterizerBase, self).setup_from

    @setup_from.setter
    def setup_from(self, value):
        super(CloudParameterizerBase, self.__class__).setup_from.fset(
            self, value)

    @property
    def sql_dtypes(self):
        import sqlalchemy
        ret = super(CloudParameterizerBase, self).sql_dtypes
        if self.args_type in ['eecra', 'files']:
            ret['id'] = sqlalchemy.INTEGER
        return ret

    @property
    def stations(self):
        return self._stations

    @stations.setter
    def stations(self, stations):
        at = self.args_type
        if self.allow_files and at == 'files':
            self._stations = self.eecra_stations = stations
        elif at == 'eecra':
            self._stations = self.eecra_stations = np.asarray(stations,
                                                              dtype=int)
        elif at == 'ghcn':
            Norig = len(stations)
            df_map = self.eecra_ghcn_map()
            try:
                df_map = df_map.loc[stations].dropna()
            except KeyError:
                self._stations = df_map.index.values[:0]
                self.eecra_stations = df_map.station_id.values[:0].astype(int)
            else:
                self._stations = df_map.index.values
                self.eecra_stations = df_map.station_id.values.astype(int)
            self.logger.debug(
                'Using %i cloud stations in the %i given stations',
                len(self._stations), Norig)
        else:
            raise ValueError(
                "args_type must be one of {'eecra', 'ghcn'%s}! Not %s" % (
                    ", 'files'" if self.allow_files else '', at))

    def eecra_ghcn_map(self):
        """Get a dataframe mapping from GHCN id to EECRA station_id"""
        cls = CloudParameterizerBase
        if self.args_type == 'eecra':
            return pd.DataFrame(self.eecra_stations, columns=['station_id'],
                                index=pd.Index(self.eecra_stations, name='id'))
        try:
            return cls._eecra_ghcn_map
        except AttributeError:
            fname = osp.join(self.data_dir, 'eecra_ghcn_map.csv')
            if not osp.exists(fname):
                fname = osp.join(
                    utils.get_module_path(inspect.getmodule(cls)), 'data',
                    'eecra_ghcn_map.csv')
            cls._eecra_ghcn_map = pd.read_csv(fname, index_col='id')
        return cls._eecra_ghcn_map

    @classmethod
    def filter_stations(cls, stations):
        """Get the GHCN stations that are also in the EECRA dataset

        Parameters
        ----------
        stations: np.ndarray
            A string array with stations to use

        Returns
        -------
        np.ndarray
            The ids in `stations` that can be mapped to the eecra dataset"""
        return cls.eecra_ghcn_map().loc[stations].dropna().index.values

    @classmethod
    def _modify_parser(cls, parser):
        parser.setup_args(default_cloud_config)
        parser, setup_grp, run_grp = super(
            CloudParameterizerBase, cls)._modify_parser(parser)
        parser.pop_key('args_type', 'metavar', None)
        choices = ['ghcn', 'eecra']
        if cls.allow_files:
            choices += ['files']
            append = (
                "\n    files\n"
                "        Arguments are paths to raw EECRA files")
        else:
            append = ''
        parser.update_arg('args_type', short='at', group=setup_grp,
                          choices=choices)
        parser.append2help('args_type', append + '\nDefault: %(default)s')
        return parser, setup_grp, run_grp

    @classmethod
    @docstrings.dedent
    def from_task(cls, task, *args, **kwargs):
        """
        %(TaskBase.from_task.summary)s

        Parameters
        ----------
        %(TaskBase.from_task.parameters)s

        Other Parameters
        ----------------
        %(TaskBase.from_task.other_parameters)s
        """
        if cls.allow_files and getattr(task.task_config, 'args_type', None):
            kwargs.setdefault('args_type', task.task_config.args_type)
        return super(CloudParameterizerBase, cls).from_task(
            task, *args, **kwargs)


class HourlyCloud(CloudParameterizerBase):
    """Parameterizer that loads the hourly cloud data from the EECRA database
    """

    name = 'hourly_cloud'

    summary = 'Hourly cloud data'

    _datafile = 'hourly_cloud.csv'

    dbname = 'hourly_cloud'

    allow_files = True

    urls = {
        ((1971, 1), (1977, 4)): (
            'http://cdiac.ornl.gov/ftp/ndp026c/land_197101_197704/'),
        ((1977, 5), (1982, 10)): (
            'http://cdiac.ornl.gov/ftp/ndp026c/land_197705_198210/'),
        ((1982, 11), (1987, 6)): (
            'http://cdiac.ornl.gov/ftp/ndp026c/land_198211_198706/'),
        ((1987, 7), (1992, 2)): (
            'http://cdiac.ornl.gov/ftp/ndp026c/land_198707_199202/'),
        ((1992, 3), (1996, 12)): (
            'http://cdiac.ornl.gov/ftp/ndp026c/land_199203_199612/'),
        ((1997, 1), (2009, 12)): (
            'http://cdiac.ornl.gov/ftp/ndp026c/land_199701_200912/')}

    mon_map = dict(zip(
        range(1, 13),
        "JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split()))

    _continue = False

    @property
    def sql_dtypes(self):
        import sqlalchemy
        ret = super(HourlyCloud, self).sql_dtypes
        ret.update({"IB": sqlalchemy.SMALLINT,
                    "lat": sqlalchemy.REAL,
                    "lon": sqlalchemy.REAL,
                    "station_id": sqlalchemy.INTEGER,
                    "LO": sqlalchemy.SMALLINT,
                    "ww": sqlalchemy.SMALLINT,
                    "N": sqlalchemy.SMALLINT,
                    "Nh": sqlalchemy.SMALLINT,
                    "h": sqlalchemy.SMALLINT,
                    "CL": sqlalchemy.SMALLINT,
                    "CM": sqlalchemy.SMALLINT,
                    "CH": sqlalchemy.SMALLINT,
                    "AM": sqlalchemy.REAL,
                    "AH": sqlalchemy.REAL,
                    "UM": sqlalchemy.SMALLINT,
                    "UH": sqlalchemy.SMALLINT,
                    "IC": sqlalchemy.SMALLINT,
                    "SA": sqlalchemy.REAL,
                    "RI": sqlalchemy.REAL,
                    "SLP": sqlalchemy.REAL,
                    "WS": sqlalchemy.REAL,
                    "WD": sqlalchemy.SMALLINT,
                    "AT": sqlalchemy.REAL,
                    "DD": sqlalchemy.REAL,
                    "EL": sqlalchemy.SMALLINT,
                    "IW": sqlalchemy.SMALLINT,
                    "IP": sqlalchemy.SMALLINT})
        return ret

    years = range(1971, 2010)

    months = range(1, 13)

    @property
    def raw_dir(self):
        """The directory where we expect the raw files"""
        return osp.join(self.data_dir, 'raw')

    @property
    def data_dir(self):
        return osp.join(super(HourlyCloud, self).data_dir, 'eecra')

    @property
    def raw_src_files(self):
        src_dir = osp.join(self.data_dir, 'raw')
        return OrderedDict([
            (yrmon, osp.join(src_dir, self.eecra_fname(*yrmon)))
            for yrmon in product(self.years, self.months)])

    @property
    def src_files(self):
        src_dir = osp.join(self.data_dir, 'stations')
        return [
            osp.join(src_dir, str(s) + '.csv') for s in self.eecra_stations]

    @classmethod
    @docstrings.get_sectionsf('HourlyCloud.eecra_fname')
    @docstrings.dedent
    def eecra_fname(cls, year, mon, ext=''):
        """The the name of the eecra file

        Parameters
        ---------
        year: int
            The year of the data
        month: int
            The integer of the month between 1 and 12"""
        c_mon = cls.mon_map[mon]
        c_yr = str(year)
        return c_mon + c_yr[-2:] + 'L' + ext

    @classmethod
    @docstrings.dedent
    def get_eecra_url(cls, year, mon):
        """
        Get the download path for the file for a specific year and month

        Parameters
        ----------
        %(HourlyCloud.eecra_fname.parameters)s
        """
        for (d0, d1), url in cls.urls.items():
            if (year, mon) >= d0 and (year, mon) <= d1:
                return url + cls.eecra_fname(year, mon, '.Z')

    def _download_worker(self, qin, qout):
        while self._continue:
            yrmon, fname = qin.get()
            utils.download_file(self.get_eecra_url(*yrmon), fname)
            spr.call(['gzip', '-d', fname])
            qout.put((yrmon, osp.splitext(fname)[0]))
            qin.task_done()

    def init_from_scratch(self):
        """Reimplemented to download the data if not existent"""
        if self.args_type == 'files':
            return
        logger = self.logger
        logger.debug('Initializing %s', self.name)
        stations = self.stations
        logger.debug('Reading data for %s stations', len(stations))
        raw_dir = self.raw_dir
        stations_dir = osp.join(self.data_dir, 'stations')
        if not osp.isdir(raw_dir):
            os.makedirs(raw_dir)
        if not osp.isdir(stations_dir):
            os.makedirs(stations_dir)
        src_files = self.src_files
        eecra_stations = self.eecra_stations
        missing = [station_id for station_id, fname in zip(
            eecra_stations, src_files) if not osp.exists(fname)]
        if missing:
            logger.debug(
                'files for %i stations are missing. Start extraction...',
                len(missing))
            from gwgen.parse_eecra import extract_data
            self.download_src(raw_dir)
            extract_data(missing, raw_dir, stations_dir,
                         self.years, self.months)
            logger.debug('Done')

    def get_data_from_files(self, files):
        def save_loc(fname):
            try:
                return pd.read_csv(fname, index_col='station_id').loc[
                    station_ids]
            except KeyError:
                return pd.DataFrame()
        station_ids = self.eecra_stations
        self.logger.debug('Extracting data for %i stations from %i files',
                          len(station_ids), len(files))
        return pd.concat(
            list(map(save_loc, files)), ignore_index=False, copy=False)

    def download_src(self, src_dir, force=False, keep=False):
        """Download the source files from the EECRA ftp server"""
        logger = self.logger
        logger.debug('    Expected data source: %s', src_dir)
        files = self.raw_src_files
        missing = {yrmon: fname for yrmon, fname in six.iteritems(files)
                   if not osp.exists(fname)}
        logger.debug('%i raw source files are missing.', len(missing))
        for yrmon, fname in six.iteritems(missing):
            compressed_fname = fname + '.Z'
            if force or not osp.exists(compressed_fname):
                utils.download_file(self.get_eecra_url(*yrmon),
                                    compressed_fname)
            spr.call(['gzip', '-d', compressed_fname] + (['-k'] * keep))

    def setup_from_file(self, *args, **kwargs):
        kwargs['index_col'] = ['id', 'year', 'month', 'day', 'hour']
        return super(HourlyCloud, self).setup_from_file(*args, **kwargs)

    def setup_from_db(self, *args, **kwargs):
        kwargs['index_col'] = ['id', 'year', 'month', 'day', 'hour']
        return super(HourlyCloud, self).setup_from_db(*args, **kwargs)

    def setup_from_scratch(self):
        """Set up the data"""
        if self.args_type == 'files':
            from gwgen.parse_eecra import parse_file
            self.data = pd.concat(list(map(parse_file, self.stations))).rename(
                columns={'station_id': 'id'})
            self.data.set_index(['id', 'year', 'month', 'day', 'hour'],
                                inplace=True)
            self.data.sort_index(inplace=True)
        else:
            files = self.src_files
            df_map = pd.DataFrame(
                np.vstack([self.stations, self.eecra_stations]).T,
                columns=['id', 'station_id'])
            self.data = pd.concat(
                [pd.read_csv(fname).merge(df_map, on='station_id', how='inner')
                 for fname in files], ignore_index=False, copy=False)
            self.data.set_index(['id', 'year', 'month', 'day', 'hour'],
                                inplace=True)
            self.data.sort_index(inplace=True)


class DailyCloud(CloudParameterizerBase):
    """Parameterizer to calculate the daily cloud values from hourly cloud data
    """

    name = 'daily_cloud'

    summary = 'Calculate the daily cloud values from hourly cloud data'

    _datafile = 'daily_cloud.csv'

    dbname = 'daily_cloud'

    setup_requires = ['hourly_cloud']

    allow_files = True

    @staticmethod
    def calculate_daily(df):
        ww = df.ww.values
        at = df.AT
        return pd.DataFrame.from_dict(OrderedDict([
            ('wet_day', [(
                ((ww >= 50) & (ww <= 75)) |
                (ww == 75) |
                (ww == 77) |
                (ww == 79) |
                ((ww >= 80) & (ww <= 99))).any().astype(int)]),
            ('tmin', [at.min()]),
            ('tmax', [at.max()]),
            ('mean_cloud', [df.N.mean() / 8.]),
            ('wind', [df.WS.mean()])
            ]))

    def setup_from_file(self, *args, **kwargs):
        kwargs['index_col'] = ['id', 'year', 'month', 'day']
        return super(DailyCloud, self).setup_from_file(*args, **kwargs)

    def setup_from_db(self, *args, **kwargs):
        kwargs['index_col'] = ['id', 'year', 'month', 'day']
        return super(DailyCloud, self).setup_from_db(*args, **kwargs)

    def setup_from_scratch(self):
        df = self.hourly_cloud.data
        data = df.groupby(level=['id', 'year', 'month', 'day']).apply(
            self.calculate_daily)
        data.index = data.index.droplevel(-1)
        self.data = data


class MonthlyCloud(CloudParameterizerBase):
    """Parameterizer to calculate the monthly cloud values from daily cloud"""

    name = 'monthly_cloud'

    summary = 'Calculate the monthly cloud values from daily cloud data'

    _datafile = 'monthly_cloud.csv'

    dbname = 'monthly_cloud'

    setup_requires = ['daily_cloud']

    allow_files = True

    @property
    def sql_dtypes(self):
        import sqlalchemy
        ret = super(MonthlyCloud, self).sql_dtypes
        for col in set(self.data.columns).difference(ret):
            if 'complete' in col:
                ret[col] = sqlalchemy.BOOLEAN
            elif 'wet_day' in col:
                ret[col] = sqlalchemy.SMALLINT
            else:
                ret[col] = sqlalchemy.REAL
        return ret

    @staticmethod
    def calculate_monthly(df):
        if len(df) > 1:
            wet = df.wet_day.values.astype(bool)
            return pd.DataFrame.from_dict(OrderedDict([
                ('wet_day', [df.wet_day.sum()]),
                ('mean_cloud_wet', [df.mean_cloud.ix[wet].mean()]),
                ('mean_cloud_dry', [df.mean_cloud.ix[~wet].mean()]),
                ('mean_cloud', [df.mean_cloud.mean()]),
                ('sd_cloud_wet', [df.mean_cloud.ix[wet].std()]),
                ('sd_cloud_dry', [df.mean_cloud.ix[~wet].std()]),
                ('sd_cloud', [df.mean_cloud.std()]),
                ]))
        else:
            return pd.DataFrame([], columns=[
                'wet_day', 'mean_cloud_wet', 'mean_cloud_dry',
                'mean_cloud', 'sd_cloud_wet', 'sd_cloud_dry', 'sd_cloud'])

    def setup_from_file(self, *args, **kwargs):
        kwargs['index_col'] = ['id', 'year', 'month']
        return super(MonthlyCloud, self).setup_from_file(*args, **kwargs)

    def setup_from_db(self, *args, **kwargs):
        kwargs['index_col'] = ['id', 'year', 'month']
        return super(MonthlyCloud, self).setup_from_db(*args, **kwargs)

    def setup_from_scratch(self):
        df = self.daily_cloud.data
        g = df.groupby(level=['id', 'year', 'month'])
        data = g.apply(self.calculate_monthly)
        # wet_day might be converted due to empty dataframe
        data['wet_day'] = data['wet_day'].astype(int)
        data.index = data.index.droplevel(-1)
        # number of records per month
        df_nums = g.count()
        df_nums['day'] = 1
        s = pd.to_datetime(df_nums.reset_index()[['year', 'month', 'day']])
        s.index = df_nums.index
        df_nums['ndays'] = ((s + pd.datetools.thisMonthEnd) - s).dt.days + 1
        cols = ['wet_day', 'tmin', 'tmax', 'mean_cloud']
        complete_cols = [col + '_complete' for col in cols]
        for col, tcol in zip(cols, complete_cols):
            df_nums[tcol] = df_nums[col] == df_nums.ndays
        df_nums.drop('day', 1, inplace=True)

        self.data = pd.merge(
            data, df_nums[complete_cols], left_index=True,
            right_index=True)


def cloud_func(x, a):
    """Function for fitting the mean of wet and dry cloud to the mean of all
    cloud

    This function returns y with

    .. math::
        y = ((-a - 1) / (a^2 x - a^2 - a)) - \\frac{1}{a}

    Parameters
    ----------
    x: np.ndarray
        The x input data
    a: float
        The parameter as mentioned in the equation above"""
    return ((-a - 1) / (a*a*x - a*a - a)) - 1/a


def cloud_sd_func(x, a):
    """Function for fitting the standard deviation of wet and dry cloud to the
    mean of wet or dry cloud

    This function returns y with

    .. math::
        y = a^2 x (1 - x)

    Parameters
    ----------
    x: np.ndarray
        The x input data
    a: float
        The parameter as mentioned in the equation above"""
    return a * a * x * (1 - x)


class CompleteMonthlyCloud(MonthlyCloud):
    """Parameterizer to extract the months with complete clouds"""

    name = 'cmonthly_cloud'

    setup_requires = ['monthly_cloud']

    dbname = 'complete_monthly_cloud'

    _datafile = 'complete_monthly_cloud.csv'

    summary = "Extract the months with complete cloud data"

    def setup_from_scratch(self):
        cols = ['wet_day', 'mean_cloud']  # not 'tmin', 'tmax'
        complete_cols = [col + '_complete' for col in cols]
        self.data = self.monthly_cloud.data[
            self.monthly_cloud.data[complete_cols].values.all(axis=1)]


class YearlyCompleteMonthlyCloud(CompleteMonthlyCloud):
    """Parameterizer to extract the months with complete clouds"""

    name = 'yearly_cmonthly_cloud'

    setup_requires = ['cmonthly_cloud']

    dbname = 'yearly_complete_monthly_cloud'

    _datafile = 'yearly_complete_monthly_cloud.csv'

    summary = "Extract the months with complete cloud data in complete years"

    allow_files = False

    def setup_from_scratch(self):
        def year_complete(series):
            """Check whether the data for the given is complete"""
            return series.astype(int).sum() == 12
        all_monthly = self.cmonthly_cloud.data

        cols = ['wet_day', 'mean_cloud']  # not 'tmin', 'tmax'

        complete_cols = [col + '_complete' for col in cols]

        df_yearly = all_monthly[complete_cols].groupby(
            level=['id', 'year']).agg(year_complete)

        names = all_monthly.index.names
        all_monthly = all_monthly.reset_index().merge(
            df_yearly[complete_cols].reset_index(), on=['id', 'year'],
            suffixes=['', '_year']).set_index(names)

        ycomplete_cols = [col + '_complete_year' for col in cols]
        self.data = all_monthly.ix[
            all_monthly[ycomplete_cols].values.all(axis=1)]


class CloudParameterizer(CompleteMonthlyCloud):
    """Parameterizer to extract the months with complete clouds"""

    name = 'cloud'

    summary = 'Parameterize the cloud data'

    setup_requires = ['cmonthly_cloud']

    _datafile = 'cloud_correlation.csv'

    dbname = 'cloud_correlation'

    allow_files = False

    fmt = dict(
        legend={'loc': 'upper left'},
        cmap='w_Reds',
        xrange=(0, 1),
        yrange=(0, 1),
        legendlabels=['std. error of a: %(a_err)1.4f'],
        bounds=['minmax', 11, 0, 99],
        cbar='',
        bins=10,
        xlabel='on %(state)s days',
        xlim=(0, 1),
        ylim=(0, 1),
        )

    has_run = True

    namelist_keys = {
        'cldf_w': 'mean_cloud_wet.a',
        'cldf_d': 'mean_cloud_dry.a',
        'cldf_sd_w': 'sd_cloud_wet.a',
        'cldf_sd_d': 'sd_cloud_dry.a'}

    error_keys = {
        'cldf_w': 'mean_cloud_wet.a_err',
        'cldf_d': 'mean_cloud_dry.a_err',
        'cldf_sd_w': 'sd_cloud_wet.a_err',
        'cldf_sd_d': 'sd_cloud_dry.a_err'}

    @property
    def sql_dtypes(self):
        import sqlalchemy
        ret = super(CloudParameterizer, self).sql_dtypes
        ret['wet_day'] = sqlalchemy.REAL
        return ret

    def setup_from_file(self, *args, **kwargs):
        kwargs['index_col'] = ['id', 'month']
        return Parameterizer.setup_from_file(self, *args, **kwargs)

    def setup_from_db(self, *args, **kwargs):
        kwargs['index_col'] = ['id', 'month']
        return Parameterizer.setup_from_db(self, *args, **kwargs)

    @property
    def ds(self):
        """The dataframe of this parameterization task converted to a dataset
        """
        import xarray as xr
        ds = xr.Dataset.from_dataframe(self.data.set_index('mean_cloud'))
        for state in ['wet', 'dry', 'all']:
            name = 'cloud' + (('_' + state) if state != 'all' else '')
            mean = 'mean_' + name
            sd = 'sd_' + name
            # set attributes
            ds[mean].attrs['long_name'] = 'mean cloud fraction'
            ds[mean].attrs['state'] = state
            ds[mean].attrs['symbol'] = 'c'
            ds[sd].attrs['long_name'] = 'std. dev. of cloud fraction'
            ds[sd].attrs['state'] = state
            ds[sd].attrs['symbol'] = 'c_\mathrm{std}'
            # set mean on wet/dry days as coordinate for std on wet/dry days
            if state != 'all':
                coord = 'c_' + mean
                ds[coord] = ds[mean].rename({'mean_cloud': coord})
                ds[sd] = ds[sd].rename({'mean_cloud': coord}).variable
        return ds

    def setup_from_scratch(self):
        g = self.cmonthly_cloud.data.groupby(level=['id', 'month'])
        data = g.mean()
        cols = [col for col in data.columns if '_complete' not in col]
        self.data = data[cols]

    @docstrings.dedent
    def create_project(self, ds):
        """
        Plot the relationship wet/dry cloud - mean cloud

        Parameters
        ----------
        %(TaskBase.create_project.parameters)s"""
        import matplotlib.pyplot as plt
        import seaborn as sns
        import psyplot.project as psy

        sns.set_style('white')

        types = ['mean', 'sd']

        axes = np.concatenate([
            plt.subplots(1, 2, figsize=(12, 4))[1] for _ in range(2)])
        for fig in set(ax.get_figure() for ax in axes):
            fig.subplots_adjust(bottom=0.25)
        middle = (
            axes[0].get_position().x0 + axes[1].get_position().x1) / 2.
        axes = iter(axes)
        fit_funcs = {'mean': cloud_func, 'sd': cloud_sd_func}
        for t in types:
            psy.plot.densityreg(
                ds, name='%s_cloud_wet' % (t), ax=next(axes),
                ylabel='%(long_name)s\non %(state)s days',
                text=[(middle, 0.03, '%(long_name)s', 'fig', dict(
                     weight='bold', ha='center'))], fmt=self.fmt,
                fit=fit_funcs[t])
            psy.plot.densityreg(
                ds, name='%s_cloud_dry' % (t), ax=next(axes),
                ylabel='on %(state)s days', fmt=self.fmt,
                fit=fit_funcs[t])
        return psy.gcp(True)[:]

    @docstrings.dedent
    def make_run_config(self, sp, info, full_nml):
        """
        Configure with the wet/dry cloud - mean cloud correlation

        Parameters
        ----------
        %(Parameterizer.make_run_config.parameters)s
        """
        nml = full_nml.setdefault('weathergen_ctl', OrderedDict())
        states = ['wet', 'dry']
        types = ['mean', 'sd']
        for t, state in product(types, states):
            vname = '%s_cloud_%s' % (t, state)
            nml_name = 'cldf%s_%s' % ("_sd" if t == 'sd' else '', state[:1])
            info[vname] = vinfo = {}
            plotter = sp(name=vname).plotters[0]
            for key in ['a', 'a_err']:
                vinfo[key] = float(plotter.plot_data[1].attrs[key])
            nml[nml_name] = float(
                plotter.plot_data[1].attrs.get('a', 0))


class CompleteDailyCloud(DailyCloud):
    """The parameterizer that calculates the days in complete months of cloud
    data"""

    name = 'cdaily_cloud'

    setup_requires = ['daily_cloud', 'monthly_cloud']

    _datafile = "complete_daily_cloud.csv"

    dbname = 'complete_daily_cloud'

    summary = "Get the days of the complete daily cloud months"

    def init_from_scratch(self):
        pass

    def setup_from_scratch(self):
        monthly = self.monthly_cloud.data
        cols = ['wet_day', 'tmin', 'tmax', 'mean_cloud']
        complete_cols = [col + '_complete' for col in cols]
        self.data = self.daily_cloud.data.reset_index().merge(
            monthly[
                monthly[complete_cols].values.all(axis=1)][[]].reset_index(),
            how='inner', on=['id', 'year', 'month'], copy=False).set_index(
                ['id', 'year', 'month', 'day'])


class YearlyCompleteDailyCloud(CompleteDailyCloud):
    """The parameterizer that calculates the days in complete months of cloud
    data"""

    name = 'yearly_cdaily_cloud'

    setup_requires = ['cdaily_cloud', 'cmonthly_cloud']

    _datafile = "yearly_complete_daily_cloud.csv"

    dbname = 'yearly_complete_daily_cloud'

    summary = (
        "Get the days of the complete daily cloud months in complete years")

    allow_files = False

    def setup_from_scratch(self):
        def year_complete(series):
            """Check whether the data for the given is complete"""
            return series.astype(int).sum() == 12
        all_monthly = self.cmonthly_cloud.data

        cols = ['wet_day', 'mean_cloud', 'tmin', 'tmax']

        complete_cols = [col + '_complete' for col in cols]

        df_yearly = all_monthly[complete_cols].groupby(
            level=['id', 'year']).agg(year_complete)

        names = all_monthly.index.names
        all_monthly = all_monthly.reset_index().merge(
            df_yearly[complete_cols].reset_index(), on=['id', 'year'],
            suffixes=['', '_year']).set_index(names)

        ycomplete_cols = [col + '_complete_year' for col in cols]
        monthly = all_monthly.ix[
            all_monthly[ycomplete_cols].values.all(axis=1)][[]].reset_index()
        self.data = self.cdaily_cloud.data.reset_index().merge(
            monthly, how='inner', on=['id', 'year', 'month'],
            copy=False).set_index(['id', 'year', 'month', 'day'])


class CrossCorrelation(Parameterizer):
    """Class to calculate the cross correlation between the variables"""

    name = 'corr'

    summary = 'Cross corellation between temperature and cloudiness'

    _datafile = 'cross_correlation.csv'

    dbname = 'cross_correlation'

    # choose only complete years to have the maximum length of consecutive
    # days
    setup_requires = ['yearly_cdaily_cloud']

    cols = ['tmin', 'tmax', 'mean_cloud']

    namelist_keys = {'a': None, 'b': None}

    # does not have stations in the index --> must be processed serial
    setup_parallel = False

    has_run = True

    @property
    def sql_dtypes(self):
        import sqlalchemy
        ret = super(CrossCorrelation, self).sql_dtypes
        for col in self.cols:
            ret[col + '1'] = ret[col]
        ret['variable'] = sqlalchemy.TEXT
        return ret

    def setup_from_file(self, **kwargs):
        """Set up the parameterizer from already stored files"""
        kwargs.setdefault('index_col', 'variable')
        self.data = pd.read_csv(self.datafile, **kwargs)
        self.data.columns.name = 'variable'

    def setup_from_db(self, **kwargs):
        """Set up the parameterizer from datatables already created"""
        kwargs.setdefault('index_col', 'variable')
        self.data = pd.read_sql_table(self.dbname, self.engine, **kwargs)
        self.data.columns.name = 'variable'

    def setup_from_scratch(self):
        df = self.yearly_cdaily_cloud.data.sort_index()
        cols = self.cols
        # m0
        self.data = final = df[cols].corr()
        final.index.name = 'variable'
        df['date'] = vals = pd.to_datetime(df[[]].reset_index().drop('id', 1))
        # make sure that the index is ignored
        df['date'].values[:] = vals
        df = df.set_index('date')
        shifted = df.shift(freq=pd.datetools.day)
        # m1
        for col in cols:
            final[col + '1'] = 0
            for col2 in cols:
                final.loc[col2, col + '1'] = df[col].corr(shifted[col2])

    def run(self, info, full_nml):
        cols = self.cols
        lag1_cols = [col + '1' for col in cols]
        nml = full_nml.setdefault('weathergen_ctl', OrderedDict())
        m0 = self.data[cols]
        m1 = self.data[lag1_cols].rename(columns=dict(zip(lag1_cols, cols)))
        m0i = np.linalg.inv(m0)
        nml['a'] = np.dot(m1, m0i).tolist()
        nml['b'] = np.linalg.cholesky(
            m0 - np.dot(np.dot(m1, m0i), m1.T)).T.tolist()
        info['M0'] = m0.values.tolist()
        info['M1'] = m1.values.tolist()

    @classmethod
    def _modify_parser(cls, parser):
        """Reimplemented because there are no run keywords for this task"""
        cls.has_run = False
        ret = super(CrossCorrelation, cls)._modify_parser(parser)
        cls.has_run = True
        return ret
