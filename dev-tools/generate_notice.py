from __future__ import print_function

import glob
import os
import datetime
import argparse
import json
import csv
import re
import pdb
import copy
import subprocess
import fnmatch

DEFAULT_BUILD_TAGS = "darwin,linux,windows"

def read_file(filename):

    if not os.path.isfile(filename):
        print("File not found {}".format(filename))
        return ""

    try:
        with open(filename, 'r') as f:
            return f.read()
    except UnicodeDecodeError:
        # try latin-1
        with open(filename, 'r', encoding="ISO-8859-1") as f:
            return f.read()


def read_go_module_deps(main_packages, build_tags):
    """
    read_go_deps returns a dictionary of modules, with the module path
    as the key and the value being a dictionary holding information about
    the module. Main modules are excluded; only dependencies are returned.

    The module dict holds the following keys:
     - Dir (required)
       Local filesystem directory holding the module contents.
       e.g. "$HOME/go/pkg/mod/github.com/elastic/go-txfile@v0.0.7"

     - Path (required)
       Module path. e.g. "github.com/elastic/beats"

     - Version (optional)
       Module version, excluding timestamp/revision and +incompatible suffix.
       If the module has a replacement, this holds the replacement module's version.

     - Revision (optional)
       VCS revision hash, extracted from module version.
       If the module has a replacement, this holds the replacement module's revision.

     - Overwrite-Path (optional)
       Replacement module path. e.g. "../beats", or "github.com/elastic/sarama".
    """
    go_list_args = ["go", "list", "-deps", "-json"]
    if build_tags:
        go_list_args.extend(["-tags", build_tags])
    output = subprocess.check_output(go_list_args + main_packages)
    modules = {}
    decoder = json.JSONDecoder()
    while True:
        output = output.strip()
        if not output:
            break
        pkg, end = decoder.raw_decode(output)
        output = output[end:]

        if 'Standard' in pkg:
            continue

        module = pkg['Module']
        if "Main" in module:
            continue

        modules[module['Path']] = module
        version = module["Version"]
        replace = module.get("Replace", None)
        del(module["Version"])
        if replace:
            if replace["Path"] != module["Path"]:
                module["Overwrite-Path"] = replace["Path"]
            # Modules with local-filesystem replacements have no version.
            version = replace.get("Version", None)

        if version:
            i = version.rfind("+incompatible")
            if i > 0:
                version = version[:i]
            version_parts = version.split("-")
            if len(version_parts) == 3: # version-timestamp-revision
                version = version_parts[0]
                module["Revision"] = version_parts[2]
            if version != "v0.0.0":
                module["Version"] = version

    return modules


def gather_modules(main_packages, build_tags, excludes):
    modules = read_go_module_deps(main_packages, build_tags)

    # walk looking for LICENSE files
    for modpath, module in modules.items():
        moddir = module['Dir']
        for root, dirs, filenames in os.walk(moddir):
            reldir = os.path.relpath(root, moddir)
            for pattern in excludes:
                for matched in fnmatch.filter(dirs, pattern):
                    dirs.remove(matched)

            for filename in get_licenses(modpath, filenames):
                license = {}
                license_path = os.path.join(root, filename)
                license["license_file"] = os.path.normpath(os.path.join(modpath, reldir, filename))
                license["license_contents"] = read_file(license_path)
                license["license_summary"] = detect_license_summary(license["license_contents"])

                notice_filenames = fnmatch.filter(filenames, "NOTICE*")
                license["notice_files"] = {
                    os.path.normpath(os.path.join(modpath, reldir, filename)): read_file(os.path.join(root, filename)) for filename in notice_filenames
                }

                if license["license_summary"] == "UNKNOWN":
                    print("WARNING: Unknown license for {}: {}".format(modpath, os.path.join(root, filename)))
                module["licenses"] = module.get("licenses", []) + [license]

    return modules


# Allow to skip files that could match the `LICENSE` pattern but does not have any license information.
SKIP_FILES = {
    # AWS lambda go defines that some part of the code is APLv2 and other on a MIT Modified license.
    "github.com/aws/aws-lambda-go": ["LICENSE-SUMMARY"]
}


def get_licenses(modpath, filenames):
    """
    Get a list of license files from a given directory.
    """
    licenses = []
    for filename in sorted(filenames):
        if filename.startswith("LICENSE") and "docs" not in filename and filename not in SKIP_FILES.get(modpath, []):
            licenses.append(filename)
        elif filename.startswith("APLv2"):  # gorhill/cronexpr
            licenses.append(filename)
        elif filename in ("COPYING",):  # BurntSushi/toml
            licenses.append(filename)
    return licenses


def write_notice_file(f, beat, copyright, modules):

    now = datetime.datetime.now()

    # Add header
    f.write("{}\n".format(beat))
    f.write("Copyright 2014-{0} {1}\n".format(now.year, copyright))
    f.write("\n")
    f.write("This product includes software developed by The Apache Software \n" +
            "Foundation (http://www.apache.org/).\n\n")

    # Add licenses for 3rd party libraries
    f.write("==========================================================================\n")
    f.write("Third party libraries used by the {} project:\n".format(beat))
    f.write("==========================================================================\n\n")

    def maybe_write(dict_, key, print_key=None):
        if key in dict_:
            f.write("{}: {}\n".format(print_key or key, dict_.get(key)))

    # Sort licenses by package path, ignore upper / lower case
    for key in sorted(modules, key=unicode.lower):
        module = modules[key]
        for lib in module.get("licenses", []):
            f.write("\n--------------------------------------------------------------------\n")
            f.write("Dependency: {}\n".format(key))
            maybe_write(module, "Version")
            maybe_write(module, "Revision")
            maybe_write(module, "Overwrite-Path", "Overwrite")
            f.write("License type (autodetected): {}\n".format(lib["license_summary"]))
            f.write("{}:\n".format(lib["license_file"]))
            f.write("--------------------------------------------------------------------\n")
            if lib["license_summary"] != "Apache-2.0":
                f.write(lib["license_contents"])
            else:
                # it's an Apache License, so include only the NOTICE file
                f.write("Apache License 2.0\n\n")

                # Skip NOTICE files which are not needed
                if lib["license_file"] in SKIP_NOTICE:
                    continue

                for notice_file, notice_contents in lib["notice_files"].items():
                    notice_file_hdr = "-------{}-----\n".format(os.path.basename(notice_file))
                    f.write(notice_file_hdr)
                    f.write(notice_contents)


def write_csv_file(csvwriter, dependencies):
    csvwriter.writerow(["name", "url", "version", "revision", "license"])
    for key in sorted(dependencies, key=unicode.lower):
        for lib in dependencies[key]:
            csvwriter.writerow([key, get_url(key), lib.get("version", ""), lib.get("revision", ""),
                                lib["license_summary"]])


def get_url(repo):
    words = repo.split("/")
    if words[0] != "github.com":
        return repo
    return "https://github.com/{}/{}".format(words[1], words[2])


def create_notice(filename, beat, copyright, csvfile, main_packages, build_tags, excludes):
    modules = gather_modules(main_packages, build_tags, excludes)

    if not csvfile:
        with open(filename, "w+") as f:
            write_notice_file(f, beat, copyright, modules)
            print("Available at {}".format(filename))
    else:
        with open(csvfile, "wb") as f:
            csvwriter = csv.writer(f)
            write_csv_file(csvwriter, modules)
            print("Available at {}".format(csvfile))
    return modules


APACHE2_LICENSE_TITLES = [
    "Apache License 2.0",
    "Apache License Version 2.0",
    "Apache License, Version 2.0",
    "licensed under the Apache 2.0 license",  # github.com/zmap/zcrypto
    re.sub(r"\s+", " ", """Apache License
    ==============

    _Version 2.0, January 2004_"""),
]

MIT_LICENSES = [
    re.sub(r"\s+", " ", """Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.
    """),
    re.sub(r"\s+", " ", """Permission to use, copy, modify, and distribute this software for any
purpose with or without fee is hereby granted, provided that the above
copyright notice and this permission notice appear in all copies."""),
    re.sub(r"\s+", " ", """Permission is hereby granted, free of charge, to any person obtaining
a copy of this software and associated documentation files (the
'Software'), to deal in the Software without restriction, including
without limitation the rights to use, copy, modify, merge, publish,
distribute, sublicense, and/or sell copies of the Software, and to
permit persons to whom the Software is furnished to do so, subject to
the following conditions:
    """),
    re.sub(r"\s+", " ", """Permission is hereby granted, free of charge, to any person obtaining a copy of this
software and associated documentation files (the "Software"), to deal in the Software
without restriction, including without limitation the rights to use, copy, modify,
merge, publish, distribute, sublicense, and/or sell copies of the Software, and to
permit persons to whom the Software is furnished to do so.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED,
INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A
PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT
HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE
SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
    """),
]

BSD_LICENSE_CONTENTS = [
    re.sub(r"\s+", " ", """Redistribution and use in source and binary forms, with or without modification,
are permitted provided that the following conditions are met:"""),
    re.sub(r"\s+", " ", """Redistributions of source code must retain the above copyright notice, this
   list of conditions and the following disclaimer."""),
    re.sub(r"\s+", " ", """Redistributions in binary form must reproduce the above copyright notice,
   this list of conditions and the following disclaimer in the documentation
   and/or other materials provided with the distribution.
""")]

BSD_LICENSE_3_CLAUSE = [
    re.sub(r"\s+", " ", """Neither the name of"""),
    re.sub(r"\s+", " ", """nor the
      names of its contributors may be used to endorse or promote products
      derived from this software without specific prior written permission.""")
]

BSD_LICENSE_4_CLAUSE = [
    re.sub(r"\s+", " ", """All advertising materials mentioning features or use of this software
   must display the following acknowledgement"""),
]

CC_SA_4_LICENSE_TITLE = [
    "Creative Commons Attribution-ShareAlike 4.0 International"
]

LGPL_3_LICENSE_TITLE = [
    "GNU LESSER GENERAL PUBLIC LICENSE Version 3"
]

MPL_LICENSE_TITLES = [
    "Mozilla Public License Version 2.0",
    "Mozilla Public License, version 2.0"
]

UNIVERSAL_PERMISSIVE_LICENSE_TITLES = [
    "The Universal Permissive License (UPL), Version 1.0"
]

ISC_LICENSE_TITLE = [
    "ISC License",
]


# return SPDX identifiers from https://spdx.org/licenses/
def detect_license_summary(content):
    # replace all white spaces with a single space
    content = re.sub(r"\s+", ' ', content)
    # replace smart quotes with less intelligent ones
    content = content.replace(b'\xe2\x80\x9c', '"').replace(b'\xe2\x80\x9d', '"')
    if any(sentence in content[0:1000] for sentence in APACHE2_LICENSE_TITLES):
        return "Apache-2.0"
    if any(sentence in content[0:1000] for sentence in MIT_LICENSES):
        return "MIT"
    if all(sentence in content[0:1000] for sentence in BSD_LICENSE_CONTENTS):
        if all(sentence in content[0:1000] for sentence in BSD_LICENSE_3_CLAUSE):
            if all(sentence in content[0:1000] for sentence in BSD_LICENSE_4_CLAUSE):
                return "BSD-4-Clause"
            return "BSD-3-Clause"
        else:
            return "BSD-2-Clause"
    if any(sentence in content[0:300] for sentence in MPL_LICENSE_TITLES):
        return "MPL-2.0"
    if any(sentence in content[0:3000] for sentence in CC_SA_4_LICENSE_TITLE):
        return "CC-BY-SA-4.0"
    if any(sentence in content[0:3000] for sentence in LGPL_3_LICENSE_TITLE):
        return "LGPL-3.0"
    if any(sentence in content[0:1500] for sentence in UNIVERSAL_PERMISSIVE_LICENSE_TITLES):
        return "UPL-1.0"
    if any(sentence in content[0:1500] for sentence in ISC_LICENSE_TITLE):
        return "ISC"

    return "UNKNOWN"


ACCEPTED_LICENSES = [
    "Apache-2.0",
    "MIT",
    "BSD-4-Clause",
    "BSD-3-Clause",
    "BSD-2-Clause",
    "MPL-2.0",
    "UPL-1.0",
    "ISC",
]
SKIP_NOTICE = []

if __name__ == "__main__":

    parser = argparse.ArgumentParser(description="Generate the NOTICE file from package dependencies")
    parser.add_argument("-b", "--beat", default="Elastic Beats",
                        help="Beat name")
    parser.add_argument("-c", "--copyright", default="Elasticsearch BV",
                        help="copyright owner")
    parser.add_argument("--csv", dest="csvfile",
                        help="Output to a csv file")
    parser.add_argument("-e", "--excludes", default=["dev-tools", "build", "vendor", "docs"],
                        help="List of top directories to exclude")
    # no need to be generic for now, no other transitive dependency information available
    parser.add_argument("-s", "--skip-notice", default=[],
                        help="List of NOTICE files to skip")
    parser.add_argument("--build-tags", default=DEFAULT_BUILD_TAGS,
                        help="Comma-separated list of build tags to pass to 'go list -deps'")
    parser.add_argument("main_package", nargs="*", default=["."],
                        help="List of main Go packages for which dependencies should be processed")
    args = parser.parse_args()

    notice = os.path.abspath("NOTICE.txt")

    excludes = args.excludes
    if not isinstance(excludes, list):
        excludes = [excludes]
    SKIP_NOTICE = args.skip_notice

    print("Get the licenses available")
    modules = create_notice(notice, args.beat, args.copyright, args.csvfile, args.main_package, args.build_tags, excludes)

    # check that all licenses are accepted
    for modpath, module in modules.items():
        licenses = module.get("licenses", None)
        if not licenses:
            raise Exception("Missing license in module: {}".format(modpath))
        for license in licenses:
            if license["license_summary"] not in ACCEPTED_LICENSES:
                raise Exception("Dependency {} has invalid {} license: {}"
                                .format(modpath, license["license_summary"], license["license_file"]))
