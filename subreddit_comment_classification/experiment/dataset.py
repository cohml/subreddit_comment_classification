"""
Object representing a dataset and all its partitions.
"""

import dask.dataframe as dd
import numpy as np
import pandas as pd

from pathlib import Path
from typing import List, Optional, Union

from scipy.sparse.base import spmatrix
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import LabelBinarizer

from experiment.available import AVAILABLE
from experiment.config import Config
from preprocess.base import MultiplePreprocessorPipeline
from util.defaults import DEFAULTS
from util.io import FEATURE_LOADERS, load_raw_data
from util.misc import pretty_dumps


class Dataset:
    """The total set of all comments used to train and evaluate a model."""

    def __init__(self, config: Config) -> None:
        """
        Read in data and return features.

        Parameters
        ----------
        config : Config
            an object enumerating all parameters for your experiment
        """

        self.load_raw_data(config)
        self.preprocess(config)

        self.labels = LabelBinarizer().fit_transform(self.raw_data.subreddit)
        self.categorical_labels = self.raw_data.subreddit
        self.size = self.preprocessed_data.index.size.compute()

        if config.features_file is not None:
            self.load_features(config)
        else:
            self.extract_features(config)

        self.describe()
        self.partition(config)


    def describe(self) -> None:     # TODO

        # `self.features`, `self.labels`, and `self.categorical_labels` will all be
        # defined before this is called

        # `self.features` will be a `np.ndarray` or a scipy spare matrix, `self.labels`
        # will be a `np.ndarray`, and `self.categorical_labels` will be a `dask.Series`

        # in this method, just compute some summary statistics about distributions of
        # classes and such, whatever i want to know in order to understand my model's
        # performance better (see notes on phone for many ideas)

        # these stats can be referenced when creating the `Report` object

        pass


    def extract_features(self, config: Config) -> None:
        """Extract features as specified in the config."""

        extractors = AVAILABLE['FEATURE_EXTRACTORS']
        extractor = extractors.get(config.extractor)

        if extractor is None:
            err = (f'"{config.extractor}" is not a recognized feature extractor. '
                   f'Please select one from the following: {pretty_dumps(extractors)}')
            raise KeyError(err)

        extractor = extractor(**config.extractor_kwargs)
        self.features = extractor.fit_transform(self.preprocessed_data)


    def load_features(self, config: Config) -> None:
        """Read in feature values from the specified file."""

        # see imported `FEATURE_LOADERS` object for bespoke feature-loading functions,
        # which vary by feature extractor

        err = 'Loading features from a file is not yet implemented.'
        raise NotImplementedError(err)

        self.features = None


    def load_raw_data(self, config: Config) -> None:
        """Read and merge all raw data in the specified directory/files."""

        raw_data_columns = ['body', 'subreddit']
        raw_data_source = config.raw_data_directory or config.raw_data_filepaths
        self.raw_data = load_raw_data(raw_data_source, columns=raw_data_columns)


    def partition(self, config: Config) -> None:
        """Split data into train and test sets."""

        indices = self.preprocessed_data.index.compute()
        train, test = train_test_split(indices, **config.train_test_split_kwargs)

        self.train_set = Partition(self.features[train],
                                   self.labels[train],
                                   self.raw_data.loc[train, 'subreddit'],
                                   indices=train)

        self.test_set = Partition(self.features[test],
                                  self.labels[test],
                                  self.raw_data.loc[test, 'subreddit'],
                                  indices=test)


    def preprocess(self, config: Config) -> None:
        """Apply preprocessing steps as specified in the config."""

        if 'preprocessing_pipeline' in config:

            pipelines = AVAILABLE['PREPROCESSING']['PIPELINES']
            (pipeline_name, pipeline_verbosity), = config.preprocessing_pipeline.items()
            pipeline = pipelines.get(pipeline_name)

            if pipeline is None:
                err = (f'"{pipeline}" is not a recognized preprocessing pipeline. '
                       f'Please select one from the following: {pretty_dumps(pipelines)}')
                raise KeyError(err)

            pipeline = pipeline(**pipeline_verbosity)

        else:

            preprocessors = AVAILABLE['PREPROCESSING']['PREPROCESSORS']
            initialized_preprocessors = []

            for preprocessor in config.preprocessors:
                (preprocessor_name, preprocessor_kwargs), = preprocessor.items()
                preprocessor = preprocessors.get(preprocessor_name)

                if preprocessor is None:
                    err = (f'"{preprocessor_name}" is not a recognized preprocessor. '
                           f'Please select from the following: {pretty_dumps(preprocessors)}')
                    raise KeyError(err)

                preprocessor = preprocessor(**preprocessor_kwargs)
                initialized_preprocessors.append(preprocessor)

            pipeline = MultiplePreprocessorPipeline(*initialized_preprocessors)

        self.preprocessed_data = pipeline.preprocess(self.raw_data.body)


class Partition(Dataset):
    """A subset of the total dataset, for example a train or test set."""

    def __init__(self,
                 features: Union[np.ndarray, spmatrix],
                 labels: np.ndarray,
                 categorical_labels: dd.Series,
                 indices: pd.core.indexes.numeric.Int64Index):
        """
        Create specialized `Dataset` object for the train or test set partitions.

        Parameters
        ----------
        features : Union[np.ndarray, spmatrix]
            extracted features for the partition
        labels : np.ndarray
            binarized labels for the partition
        categorical_labels : dd.Series
            subreddit names for each sample in the partition; useful for `describe`
        indices : pd.core.indexes.numeric.Int64Index
            indices of samples selected for the partition
        """

        self.features = features
        self.labels = labels
        self.categorical_labels = categorical_labels
        self.indices = indices
        self.size = indices.size

        self.describe()
