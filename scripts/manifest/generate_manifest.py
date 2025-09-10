"""Creates a manifest file from a list of snapshots sourced from an web page"""
import json
import re
import sys
import subprocess
import argparse
import logging
import requests
from s3Interface import S3Interface
from bs4 import BeautifulSoup

# 1 - Connect to S3 and parse out list of snapshots
#   - Alternatively connect to web page and parse out list of snapshots
# 2 - process list and create data structure, snapshot_manifest
# python3.10 generate_manifest_from_eosnation.py --source-net mainnet --spring-version 5.0.2

class ParseSnapshots:
    """Parse HTML page with list of snapshots"""
    def __init__(self, url, header_title):
        self.url = url
        self.header_title = header_title

    # needed my own integer check function
    # `isinstance` was too strict
    # pylint prefered `isinstance` over `type` check
    @staticmethod
    def is_integer(string):
        """custom check if is int"""
        if string is None:
            return False

        if isinstance(string, int):
            return True

        # Define a regex pattern for a positive integer
        pattern = r'^\d+$'
        match = re.match(pattern, string)

        return match is not None

    @staticmethod
    def parse_block(url):
        """Parse block from snapshot url"""
        # Split the string by either '-' or '.'
        parts = re.split(r'[-.]', url)
        # 3rd to last is the start block
        if len(parts) >= 3:
            if ParseSnapshots.is_integer(parts[-3]):
                return int(parts[-3])
        return None

    def get_content(self):
        """wrapper function to parse HTML and return snapshots as a list"""
        return self.get_urls(self.get_card_content())

    def get_card_content(self):
        """parses cards and finds the returns HTML body"""
        # Make a GET request to the URL
        response = requests.get(self.url, timeout=3)
        if response.status_code != 200:
            logging.debug("Failed to fetch the URL")
            sys.exit(1)

        # Parse the HTML content
        soup = BeautifulSoup(response.content, 'html.parser')

        # Find all divs with class "card-header-title"
        all_cards = soup.find_all("div", class_="card")
        for card in all_cards:
            # Find the 'header' element with class 'card-header'
            # that contains a 'p' element with class 'card-header-title'
            card_header = card.find("header", class_="card-header")
            if card_header:
                card_title = card_header.find("p", class_="card-header-title")
                if card_title and self.header_title in card_title.get_text().strip():
                    card_content = card_header.find_next_sibling("div", class_="card-content")
                    if card_content:
                        return card_content
        return None

    def get_urls(self, card_content):
        """given an html list parse it and return the URLs"""
        url_list = []
        if card_content is None:
            return url_list

        snapshot_list = card_content.find_all("li")
        if snapshot_list:
            for element in snapshot_list:
                a_tag = element.find("a")
                if a_tag:
                    url = a_tag.get("href")
                    if url:
                        url_list.append(url)

        # add one fake snapshot for genesis
        url_list.append("https://ops.store.eosnation.io/eos-snapshots/snapshot-2018-06-10-01-eos-v6-0000000000.bin.zst") # pylint: disable=line-too-long
        return url_list

    @staticmethod
    def filter_by_block_range(snapshots, min_block_num, max_block_num):
        """filter out snapshots outside the given range"""
        index = 0
        number_of_snapshots = len(snapshots)
        filter_snapshots = []

        while index < number_of_snapshots:
            start_block_num = ParseSnapshots.parse_block(snapshots[index])
            # if we can't parse block num keep it
            # if max or min not defined that side is unbounded
            if not start_block_num:
                filter_snapshots.append(snapshots[index])
            else:
                if (not max_block_num or start_block_num < max_block_num) \
                    and (not min_block_num or start_block_num > min_block_num):
                    filter_snapshots.append(snapshots[index])
                else:
                    logging.debug("FILTER: removing block range staring at %i", start_block_num)
            index += 1

        return filter_snapshots

class BuildSnapshotsFromS3: # pylint: disable=too-few-public-methods
    """Build URL list of snapshots from S3 repo"""
    def __init__(self, bucket, snap_dir):
        self.bucket = bucket
        self.snap_dir = snap_dir

    def get_urls(self):
        """builds a list of URLs from snapshots in S3 Bucket"""
        logging.debug("s3 path %s",
            S3Interface.build_s3_loc(self.bucket,self.snap_dir+'/snapshots/'))
        snaps_list  = S3Interface.list(self.bucket, self.snap_dir +'/snapshots/', True)
        url_list = []
        # add one fake snapshot for genesis
        url_list.append("https://eosio.null.com/chicken/snapshot-2018-06-10-01-eos-v6-0000000000.bin.zst") # pylint: disable=line-too-long
        for snapshot in snaps_list:
            url_list.append("https://eosio.null.com/chicken/"+snapshot)
        return url_list


class Manifest:
    """Builds manifest and prints json config from list of snapshots"""
    def __init__(self, snapshots, source_net, leap_verison, min_block_increment=5000):
        self.snapshots = snapshots
        self.source_net = source_net
        self.spring_version = leap_verison
        self.min_block_increment = min_block_increment
        self.block_heights = []
        self.manifest = []
        # create manifest
        self.build()
        # remove any invalid records
        self.remove_invalid_records()
        # now remove snapshots that refer to invalid records
        self.clean_snapshot_list()
        # ensure slices are not too frequent
        self.space_out_slices()

    def url_to_s3_path(self, url):
        """convert URL to our S3 Path"""
        file_parts = url.split('/')
        if len(file_parts) >= 1:
            return f"s3://chicken-dance/{self.source_net}/snapshots/{file_parts[-1]}"

        return None

    def build(self):
        """constructs the manifest"""

        for url in self.snapshots:
            record = {
                'start_block_id': None,
                'end_block_id': None,
                'snapshot_path': None,
                'storage_type': 's3',
                'expected_integrity_hash': '',
                'spring_version': self.spring_version
            }
            start_block_num = ParseSnapshots.parse_block(url)
            if start_block_num or start_block_num == 0:
                record['start_block_id'] = start_block_num
                # list of block heights will use this to calc end_block_id
                self.block_heights.append(start_block_num)

            s3_snapshot_path = self.url_to_s3_path(url)
            if s3_snapshot_path:
                record['snapshot_path'] = s3_snapshot_path

            self.manifest.append(record)

        # sort array desending
        self.block_heights.sort()

        # second pass to calculate end blocks
        for record in self.manifest:
            index = 0
            number_of_records = len(self.block_heights)

            # block_heights is a sorted array.
            # when the block height matches the start_block_id
            # the very next element in block_heights is the end_block_id
            # wrap with check to make sure index is not out of bounds
            while index < number_of_records:
                if self.block_heights[index] == record['start_block_id']:
                    if index + 1 < number_of_records:
                        record['end_block_id'] = self.block_heights[index+1]
                    # found match end this while loop
                    break
                index += 1

    def remove_invalid_records(self):
        """remove all items that are not valid, typically very first and last"""
        self.manifest = [rc for rc in self.manifest \
          if (rc['start_block_id'] or rc['start_block_id'] == 0) \
          and rc['end_block_id'] and rc['snapshot_path'] \
          and rc['start_block_id'] < rc['end_block_id']]

    def clean_snapshot_list(self):
        """iterate through URL snapshot and rm entries that are not in manifest"""

        # if manifest does not exist no need to run exit early
        if len(self.manifest) == 0:
            return

        # iterate over snapshots
        # at end delete records with no matches
        index = 0
        number_of_snapshots = len(self.snapshots)
        while index < number_of_snapshots:
            has_match = False
            url_file_parts = self.snapshots[index].split('/')
            if len(url_file_parts) >= 1:
                snapshot_file = url_file_parts[-1]
            # iterate over known good manifest list
            for record in self.manifest:
                record_file_parts = record['snapshot_path'].split('/')
                if len(record_file_parts) >= 1:
                    # match we are done searching
                    # set flag and exit loop
                    if snapshot_file == record_file_parts[-1]:
                        has_match = True
                        break
            if not has_match:
                number_of_snapshots -= 1
                del self.snapshots[index]

            index += 1

    def space_out_slices(self):
        """at least min blocks between slices"""
        index = 0
        self.block_heights.sort()
        number_of_slices = len(self.block_heights)

        rm_block_list = []
        previous_good_block_height = None

        while index < number_of_slices:
            # first iteration previous_good_block_height is None and not valid
            if previous_good_block_height:
                next_desired_block_height = previous_good_block_height + self.min_block_increment
                # too small remove it
                # otherwise treat as new good_block_height
                # decrement size to account for removal
                if self.block_heights[index] < next_desired_block_height:
                    if index+1 < number_of_slices:
                        rm_block_list.append(
                            {'start': self.block_heights[index],
                            'end': self.block_heights[index+1]}
                            )
                        number_of_slices -= 1
                        del self.block_heights[index]
                else:
                    previous_good_block_height = self.block_heights[index]
            else:
                previous_good_block_height = self.block_heights[index]
            index += 1

        # build up new manifest
        sparse_manifest = []

        for record in self.manifest:
            # remapp end block for continuity as we remove slices
            logging.debug("SPACEOUT: processing record with %i to %i ...",
                record['start_block_id'], record['end_block_id'])
            add_record = True

            for blocks in rm_block_list:
                rm_start_block = blocks['start']
                new_end = blocks['end']

                if record['end_block_id'] == rm_start_block:
                    logging.debug("SPACEOUT: remapping end %s to %s",
                        rm_start_block, new_end)
                    record['end_block_id'] = new_end
                # remove sections too frequent
                if record['start_block_id'] == rm_start_block:
                    logging.debug("SPACEOUT: triming, removing record with %i to %i",
                        record['start_block_id'], record['end_block_id'])
                    add_record = False

            if add_record:
                logging.debug("SPACEOUT:... adding record with %i to %i",
                    record['start_block_id'], record['end_block_id'])
                sparse_manifest.append(record)
        # update at end
        self.manifest = sparse_manifest


    def __str__(self):
        return json.dumps(self.manifest, indent=4)

    def upload_snapshots(self):
        """upload snapshots to cloud storage"""
        for url in self.snapshots:
            s3_snapshot_path = self.url_to_s3_path(url)
            if s3_snapshot_path:
                # test does file exist already
                s3_parts = s3_snapshot_path.split('/')
                if len(s3_parts) > 3:
                    bucket  = s3_parts[2]
                    s3_path = '/'.join(s3_parts[3:])
                    download_file = "/tmp/snapshot.bin.zst"
                    # build commands
                    s3_file_exists = ["aws", "s3api", "head-object", \
                        "--bucket", bucket, "--key", s3_path]
                    download_cmd = ["curl", "-s", "-o", download_file, url]
                    upload_cmd = ["aws", "s3", "cp", download_file, s3_snapshot_path]
                    remove_cmd = ["rm", download_file]
                    # execute
                    exists_result = subprocess.run(s3_file_exists, \
                        check=False, capture_output=True, text=True)
                    if exists_result.returncode != 0:
                        # file does not exist proceed
                        logging.debug("UPLOAD: %s does not exist in cloud store", s3_path)
                        download_result = subprocess.run(download_cmd, \
                            check=True, capture_output=True, text=True)
                        # check if download succeeded
                        if download_result.returncode != 0:
                            logging.debug("UPLOAD: unable to download %s %s",
                                url, download_result.stderr)
                        else:
                            # now upload to cloud and remove from localhost
                            subprocess.run(upload_cmd, check=False)
                            subprocess.run(remove_cmd, check=False)
                            logging.debug("UPLOAD: successfully uploaded %s", s3_path)
                    else:
                        logging.debug("UPLOAD: file %s already exists nothing to upload", s3_path)

if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='helper script to build manifest that drives chicken dance')
    parser.add_argument('--source-net', type=str, default='mainnet',
        help='which chain to use e.g. mainnet or jungle')
    parser.add_argument('--snapshot-version', type=str, default='v6',
        help='version of snapshot, default v6')
    parser.add_argument('--spring-version', type=str, default='5.0.0',
        help='version of leap, default 5.0.0')
    parser.add_argument('--source-eosnation', action=argparse.BooleanOptionalAction, \
        default=False, help='build snapshots from eosnation snapshots list')
    parser.add_argument('--block-space-between-slices', type=int, default=5000, \
        help='min number of blocks between slices, cuts down on the number of slices created')
    parser.add_argument('--upload-snapshots', action=argparse.BooleanOptionalAction, \
        default=False, help='upload snapshot to cloud storage, warning this takes time')
    parser.add_argument('--max-block-height', type=int, default=None, \
        help='limits manifest by not processing starting block ranges above value')
    parser.add_argument('--min-block-height', type=int, default=None, \
        help='limits manifest by not processing starting block ranges below value')
    parser.add_argument('--debug', action=argparse.BooleanOptionalAction, \
        default=False, help='print debug stmts to stderr')

    args = parser.parse_args()

    if args.debug:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.ERROR)

    list_of_snapshots = []
    # execute code to source snapshot list from eosnation
    # otherwise default is to pull snapshot list from S3 snapshot
    if args.source_eosnation:
        search_title = f"{args.source_net} - {args.snapshot_version}"
        if args.source_net.lower() == "mainnet":
            search_title = f"Vaulta Mainnet - {args.snapshot_version}"
        if "jungle" in args.source_net.lower():
            search_title = f"Jungle 4 Testnet - {args.snapshot_version}"
        if "kylin" in args.source_net.lower():
            search_title = f"Kylin Testnet - {args.snapshot_version}"

        parser = ParseSnapshots("https://snapshots.eosnation.io/", search_title)

    else:
        builder = BuildSnapshotsFromS3("chicken-dance",args.source_net.lower())
        list_of_snapshots = builder.get_urls()

    # filter only when max or min defined
    if args.min_block_height or args.max_block_height:
        list_of_snapshots = ParseSnapshots.filter_by_block_range(
            list_of_snapshots,
            args.min_block_height,
            args.max_block_height
        )
    else:
        logging.debug("FILTER: no filter to run")

    # hmm something went wrong
    if not list_of_snapshots:
        logging.error("Failed to match, no sources found")
        sys.exit(1)

    # strip out whitespace to get name of chain
    CHAIN_NAME = ''.join(args.source_net.lower().split())
    manifest = Manifest(list_of_snapshots, CHAIN_NAME, args.spring_version.lower(), \
                        args.block_space_between_slices)

    if args.upload_snapshots:
        logging.warning("uploading snapshots: warning this can take time")
        manifest.upload_snapshots()
    print(manifest)
