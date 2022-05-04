"""
Use PRAW to pull comments from top and hot posts in one or more specified subreddits
and write the results to a series of .parquet files, one per subreddit.
"""

import argparse
import logging
import logging.config
import pandas as pd
import praw

from datetime import datetime
from pathlib import Path
from prawcore.exceptions import Forbidden
from time import sleep
from typing import Iterable, List, Optional

from ..utils.defaults import DEFAULTS
from ..utils.login import Reddit
from ..utils.misc import full_path


# set up logging
logging.config.fileConfig(DEFAULTS['PATHS']['FILES']['LOG_CONFIG'])
logger = logging.getLogger(__name__)
formatter = logging.Formatter(DEFAULTS['LOG']['FORMAT'])


def get_subreddits(subreddits_filepath: Path) -> List[str]:
    """
    Read list of subreddits to scrape comment data from.

    Parameters
    ----------
    subreddits_filepath : Path
        path to file enumerating subreddits to scrape comment data from

    Returns
    -------
    subreddits : List[str]
        names of subreddits to scrape comment data from
    """

    return subreddits_filepath.read_text().splitlines()


def parse_args() -> argparse.Namespace:
    """
    Parse command line arguments.

    Returns
    -------
    args : argparse.Namespace
    """

    parser = argparse.ArgumentParser(
        description='Scrape comments on top and hot Reddit posts in passed subreddits.'
    )
    subreddits = parser.add_mutually_exclusive_group()
    subreddits.add_argument(
        '-s', '--subreddits',
        nargs='+',
        help='whitespace-delimited list of subreddits to scrape; passed names are '
             'case-sensitive and must exactly match how they appear on Reddit'
    )
    subreddits.add_argument(
        '-f', '--subreddits-filepath',
        type=full_path,
        default=DEFAULTS['PATHS']['FILES']['MY_SUBREDDITS_FILE'],
        help='path to file enumerating subreddits to scrape comment data from '
             '(default: %(default)s)'
    )
    parser.add_argument(
        '-o', '--output-directory',
        type=full_path,
        default=DEFAULTS['PATHS']['DIRS']['ALL_FIELDS'],
        help='path to directory to write output files to (one per subreddit)'
             '(default: %(default)s)'
    )
    parser.add_argument(
        '-l', '--log-filepath',
        type=full_path,
        help='path to log file; if unspecified, all logging is printed to stdout but '
             'not saved to a file'
    )
    parser.add_argument(
        '-m', '--limit',
        type=int,
        help='maximum number of comments to scrape from each of the "top" and "hot" '
             'categories; if unspecified, as many as possible will be scraped, up to '
             '1000 per category (default: %(default)s)'
    )
    parser.add_argument(
        '-p', '--sleep-duration',
        type=int,
        default=5,
        help='number of minutes to sleep between scraping subs (default: '
             '%(default)s)'
    )
    parser.add_argument(
        '-r', '--resume',
        action='store_true',
        help='skip subreddits which have already been scraped (pass this if '
             'the previous attempt to scrape terminated early, e.g. '
             'RecursionError) (default: %(default)s)'
    )
    return parser.parse_args()


def scrape_posts(reddit: praw.Reddit,
                 subreddit: str,
                 subreddit_counter: str,
                 limit: Optional[int]
                 ) -> Optional[List[praw.reddit.Submission]]:
    """
    Scrape up to 1000 top + hot posts from each of the passed subreddits.

    Parameters
    ----------
    reddit : praw.Reddit
        a Reddit instance
    subreddit : str
        display name of a single subreddit
    subreddit_counter : str
        string representation of number of subreddits scraped so far
    limit : Optional[int]
        maximum number of comments to scrape from each of the "top" and "hot" categories

    Returns
    -------
    posts : Optional[List[praw.reddit.Submission]]
        scraped posts; will be `NoneType` if subreddit cannot be scraped (e.g.,
        subreddit is private, or the name is misspelled)
    """

    logger.info(f'Scraping subreddit "{subreddit}" ({subreddit_counter})')

    try:
        top_posts = reddit.subreddit(subreddit).top(limit=limit)
        hot_posts = reddit.subreddit(subreddit).hot(limit=limit)
    except Forbidden:
        logger.error(f'ForbiddenError triggered by subreddit "{subreddit}" '
                     '-- check if private, or typo in name '
                     '-- skipping subreddit')
        return None

    posts = list(top_posts) + list(hot_posts)
    logger.info(f'Scraping subreddit "{subreddit}" ({subreddit_counter}) '
                f'-- {len(posts)} posts')

    return posts


def traverse_comment_threads(posts: List[praw.reddit.Submission],
                             subreddit_counter: str
                             ) -> List[pd.Series]:
    """
    Scrape all comments across all posts scraped from a single subreddit.

    Parameters
    ----------
    posts : List[praw.reddit.Submission]
        all posts scraped from a single subreddit
    subreddit_counter : str
        string representation of number of subreddits scraped so far

    Returns
    -------
    comments : List[pd.Series]
        all scraped comments from a single subreddit
    """

    def traverse(comment: praw.reddit.Comment) -> Iterable[pd.Series]:
        """Recursivly traverse single-comment tree of arbitrary depth."""
        if isinstance(comment, praw.models.MoreComments):
            for comment in comment.comments():
                yield from traverse(comment)

        else:
            nonlocal subreddit_counter, post_counter, num_comment
            num_comment += 1
            logger.info(f'Scraping subreddit "{post.subreddit.display_name}" ({subreddit_counter}) '
                        f'-- post "{post.id}" ({post_counter}) '
                        f'-- comment "{comment.id}" ({num_comment}/{post.num_comments})')
            yield pd.Series(vars(comment))

            try:
                for reply in comment.replies:
                    yield from traverse(reply)
            except RecursionError:  # if comment tree is deeper than Python recursion limit
                logger.warning(f'RecursionError triggered by post "{post.id}", comment "{comment.id}" '
                               '-- skipping children')
                pass

    comments = []
    num_posts = len(posts)
    scraped_posts = set()

    for num_post, post in enumerate(posts, start=1):
        post_counter = f'{num_post}/{num_posts}'

        if post.id in scraped_posts:  # in case post tagged as both "top" and "hot"
            logger.warning(f'Post "{post.id}" comments already scraped ({post_counter})'
                           '-- skipping post')
            continue

        logger.info(f'Scraping subreddit "{post.subreddit.display_name}" ({subreddit_counter}) '
                    f'-- post "{post.id}" ({post_counter}, {post.url[8:]}) '
                    f'-- {post.num_comments} comments')
        num_comment = 0

        for comment in post.comments:
            comment_thread = traverse(comment)
            comments.extend(comment_thread)

        scraped_posts.add(post.id)

    return comments


def write_to_parquet(comments: pd.DataFrame,
                     subreddit: str,
                     output_directory: Path
                     ) -> None:
    """
    Write comments from a single subreddit to a .parquet file in specified directory.
    If output filename exists, meaning comments for the passed subreddit have already
    been scraped and saved, then merge and overwrite.

    Parameters
    ----------
    comments : pd.DataFrame
        all scraped comments from a single subreddit
    subreddit : str
        display name of a single subreddit
    output_directory : Path
        path to directory to write output files to
    """

    output_file = output_directory / (subreddit + '.parquet')

    if output_file.exists():
        logger.info(f'Merging subreddit "{subreddit}" '
                    f'-- new data with existing {output_file}')
        comments = pd.concat([comments, pd.read_parquet(output_file, engine='pyarrow')])

    # drop useless columns containing praw technical metadata, and drop comments
    # scraped multiple times (which could happen if script failed previously, or
    # occasionally for a post tagged as both "top" and "new"), then write to .parquet
    logger.info(f'Writing {output_file}')
    praw_junk = {'regex' : r'^_'}
    dropcols = comments.filter(**praw_junk).columns
    (comments.drop(columns=dropcols)
             .drop_duplicates(subset='id')
             .astype({'author' : str, 'edited' : bool, 'subreddit' : str})
             .applymap(lambda x: str(x) if x in ([], {}) else x)
             .to_parquet(output_file, compression='gzip', index=False))


def main() -> None:
    # parse command line arguments
    args = parse_args()

    # make output directory if needed
    args.output_directory.mkdir(exist_ok=True, parents=True)

    # optionally log to file, appending if already exists
    if args.log_filepath:
        args.log_filepath.parent.mkdir(exist_ok=True, parents=True)
        mode = 'a' if args.log_filepath.exists() else 'w'
        log_filepath_handler = logging.FileHandler(args.log_filepath,
                                                   encoding='utf-8',
                                                   mode=mode)
        log_filepath_handler.setFormatter(formatter)
        logger.addHandler(log_filepath_handler)

    # connect to reddit
    reddit = Reddit()

    # get list of subs to scrape data from
    if args.subreddits:
        subreddits = args.subreddits
    else:
        subreddits = get_subreddits(args.subreddits_filepath)

    # optionally skip subreddits from which comments have already been scraped
    if args.resume:
        already_scraped = [p.stem for p in args.output_directory.glob('*.parquet')
                           if not p.stem.endswith('_cleaned')]
        for subreddit in sorted(already_scraped, key=str.lower):
            if subreddit in subreddits:
                logger.info(f'Skipping subreddit "{subreddit}"')
                subreddits.remove(subreddit)

    num_subreddits = len(subreddits)

    for num_subreddit, subreddit in enumerate(subreddits, start=1):
        subreddit_counter = f'{num_subreddit}/{num_subreddits}'

        # scrape posts
        posts = scrape_posts(reddit.session, subreddit, subreddit_counter, args.limit)

        # skip subreddit if error accessing its contents
        if posts is None:
            continue

        # scrape all comments across all scraped posts
        comments = traverse_comment_threads(posts, subreddit_counter)
        comments = pd.DataFrame(comments)

        # write to output
        write_to_parquet(comments, subreddit, args.output_directory)

        if args.sleep_duration > 0 and num_subreddit < num_subreddits:
            logger.info(f'Sleeping {args.sleep_duration} minutes ...')
            sleep(60 * args.sleep_duration)


if __name__ == '__main__':
    main()
