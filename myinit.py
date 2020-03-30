#!/usr/bin/python3.7
import yaml
import sys
import getopt
import os
from dataclasses import dataclass
from typing import Tuple, Callable, List, Union, IO
import contextlib
import tarfile
import subprocess
import string
import datetime
import pwd
import grp
import functools
import pathlib
import tempfile
import io
import shutil
import traceback

@dataclass
class CommandFuncEntry:
    command_names: Tuple[str]
    func: Callable

class bcolors:
    HEADER = '\033[95m'
    OKBLUE = '\033[94m'
    OKGREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'
    UNDERLINE = '\033[4m'

# ALL VARIABLE VALUES MUST BE STRING!
Consts: dict = {
    "MyInitDir": os.path.abspath(os.path.dirname(os.path.abspath(__file__))).strip("/\\") + "/",
    "TmpSystemDir": "/tmp/",
    "ExtraArchiveFilePrefix": "__extra__/",
    "AutomaticallyUseDefault": False
}

str_formatter = string.Formatter()

def do_nothing(*args, **kwargs):
    pass

def warn_color(s: str):
    return bcolors.WARNING + s + bcolors.ENDC

def info_color(s: str):
    return bcolors.OKBLUE + s + bcolors.ENDC

def error_color(s: str):
    return bcolors.FAIL + s + bcolors.ENDC


dbg_print = do_nothing
eprint = functools.partial(print, file=sys.stderr)
# dbg_print = lambda *args, **kwargs: eprint(info_color(args[0]), *args[1:], **kwargs)
print = lambda *args, **kwargs: eprint(info_color(args[0]), *args[1:], **kwargs)
warn_print = lambda *args, **kwargs: eprint(warn_color(args[0]), *args[1:], **kwargs)
error_print = lambda *args, **kwargs: eprint(error_color(args[0]), *args[1:], **kwargs)
input_old = input
input = lambda *args, **kwargs: input_old(warn_color(args[0]), *args[1:], **kwargs)

AskStorage = {}
RecognizedOpts = ["yes", "no", "exit", "all", "overwrite", "skip", "resolve"]
RecognizedOptsNotCapitalized = ["nottoall", "alwaysoverwrite", "alwaysskip", "alwaysresolve"]

def ask(storage_token: str, prompt: str, opts: List[str]):
    remembered_response: str = AskStorage.get(storage_token, None)
    if remembered_response is not None:
        return remembered_response

    capitalized_opts = []
    one_letter_opts = []
    for opt in opts:
        if opt in RecognizedOpts:
            capitalized_opts.append(opt[0].upper() + opt[1:])
            one_letter_opts.append(opt[0].lower())
        elif opt in RecognizedOptsNotCapitalized:
            capitalized_opts.append(opt)
            one_letter_opts.append(None)
        else:
            raise ValueError(f'unrecognized option: {opt}')

    while True:
        input_value = input(prompt + f'[{"/".join(capitalized_opts)}]: ')

        index: int
        if input_value == "":
            input_value = opts[0]

        if input_value.lower() in opts:
            index = opts.index(input_value.lower())
        elif input_value[0].lower() in one_letter_opts:
            index = one_letter_opts.index(input_value.lower())
        else:
            continue

        response: str = opts[index]

        if response == "exit":
            sys.exit(1)

        if response == "all":
            response = "yes"
            AskStorage[storage_token] = "yes"

        if response == "nottoall":
            response = "no"
            AskStorage[storage_token] = "no"

        if response.startswith("always"):
            response = response[len("always"):]
            AskStorage[storage_token] = response
        
        return response

def str_is_true(s: str):
    return s == "True" or s == "true" or s == "yes" or s == "Yes"

def format_value(value: str, entry: dict, config: dict, depth: int) -> str:
    dbg_print(f'formatting {value} in {entry["id"] if entry else "<No Entry>"}...')

    temp_format_dict: dict = {}
    for _, key_var_name, _, _ in str_formatter.parse(value):
        if key_var_name is None:
            continue

        var_ref: dict = {
            "refVar": key_var_name
        }
        temp_format_dict[key_var_name] = resolve_var_ref(key_var_name, var_ref, entry, config, depth + 1)

    return value.format(**temp_format_dict)

def resolve_var_ref_worker(prompt_var_name: str, var_ref: Union[str, dict], entry: dict, config: dict, depth: int = 0) -> str:
    if depth >= 100:
        raise RecursionError("resolve_var_ref: depth limit exceeded")

    dbg_print(f'resolving {prompt_var_name} in {entry["id"] if entry else "<No Entry>"}...')

    if var_ref is None:
        raise ValueError(f'{prompt_var_name} is None')

    if isinstance(var_ref, str):
        return format_value(var_ref, entry, config, depth)

    if not isinstance(var_ref, dict):
        return var_ref
    
    var_ref: dict
    final_value = var_ref.get("final_value", None)
    if final_value is not None:
        return final_value

    imm_value = var_ref.get("value", None)
    if imm_value is not None:
        if var_ref.get("doNotFormat", False) or not isinstance(imm_value, str):
            var_ref["final_value"] = imm_value
        else:
            var_ref["final_value"] = format_value(imm_value, entry, config, depth) 
        return var_ref["final_value"]

    ref_var_name = var_ref.get("refVar", None)
    if ref_var_name is not None:
        entry_scope_dict: dict
        if entry is not None:
            entry_scope_dict = entry.get("varDict", {})
        else:
            entry_scope_dict = {}

        config_scope_dict: dict
        config_scope_dict = config.get("commonVarDict", {})
        
        if ref_var_name in entry_scope_dict:
            refed_var_ref = entry_scope_dict[ref_var_name]
        elif ref_var_name in config_scope_dict:
            refed_var_ref = config_scope_dict[ref_var_name]
        elif ref_var_name in Consts:
            refed_var_ref = Consts[ref_var_name]
        else:
            raise KeyError(f'{ref_var_name} unresolved!')

        var_ref["final_value"] = resolve_var_ref(ref_var_name, refed_var_ref, entry, config, depth + 1)
        return var_ref["final_value"]

    default_value = var_ref.get("defaultValue", None)
    input_value: str = ""
    if default_value is not None:
        will_auto_resolve = resolve_var_ref("ResolvingAutomaticallyUseDefault", { "refVar": "AutomaticallyUseDefault" }, entry, config, depth + 1)

        if not will_auto_resolve:
            input_value = input(f'Input value for variable {prompt_var_name}{("(" + var_ref["description"] + ")") if "description" in var_ref else ""} [Default={default_value}]: ')
            if input_value == "":
                input_value = default_value
        else:
            input_value = default_value
    else:
        input_value = input(f'Input value for variable {prompt_var_name}{("(" + var_ref["description"] + ")") if "description" in var_ref else ""}: ')

    var_ref["final_value"] = input_value
    return var_ref["final_value"]

def resolve_var_ref(prompt_var_name: str, var_ref: Union[str, dict], entry: dict, config: dict, depth: int = 0) -> str:
    result: str = resolve_var_ref_worker(prompt_var_name, var_ref, entry, config, depth)
    if prompt_var_name.lower().endswith("dir") and (not result or result[-1] != "/"):
        ask_value: str = ask("does_not_end_with_backslash", f'{prompt_var_name} does not end with a backslash. Continue? ', [
                "yes",
                "no",
                "all",
                "exit"
            ])

        if ask_value != "yes":
            sys.exit(1)

    return result

def resolve_var_ref_in_dict_by_key(d: dict, k: str, prompt_var_name_prefix: str, entry: dict, config: dict) -> str:
    return resolve_var_ref(prompt_var_name_prefix + k, d[k], entry, config, 0)


def preprocess_config(config: dict):
    # dict-ify entries
    entries_dict = {}
    config["entries_dict"] = entries_dict
    
    for entry in config.get("entries", []):
        assert (entry["id"] not in entries_dict), "duplicate entry id: {}".format(entry["id"])
        entries_dict[entry["id"]] = entry

        if entry["type"] == "file":
            files_dict = {}
            entry["files_dict"] = files_dict
            for file in entry.get("files", []):
                archive_file_path: str = os.path.join(resolve_var_ref_in_dict_by_key(file, "archiveDir", entry["id"] + "/" + file["name"] + "/", entry, config), file["name"])
                files_dict[archive_file_path] = file
                

def read_config_in_path(path: str):
    config: dict
    with contextlib.closing(open(path, "r")) as f:
        config = yaml.safe_load(f)

    preprocess_config(config)
    return config

def read_config_in_archive(archive_path: str) -> Tuple[tarfile.TarFile, dict]:
    tar = tarfile.open(archive_path, "r:gz")
    config_member = tar.getmember("config.yaml")
    config_f = tar.extractfile(config_member)
    
    config: dict = yaml.safe_load(config_f)

    preprocess_config(config)
    config["commonVarDict"] = config.get("commonVarDict", {})
    config["commonVarDict"]["Archive"] = archive_path
    return tar, config

def get_system_file_to_read__(path: str, as_user: str) -> IO:
    # must be a file
    if as_user == Consts["CurrentUser"]:
        return open(path, "r")
    else:
        proc = subprocess.Popen(["sudo", "-u", as_user, "cat", path], stdout=subprocess.PIPE)
        proc.wait()
        return proc.stdout
        
def make_archive_filename(config: dict):
    return config["id"] + ("." + str(config["confVersion"]) if "confVersion" in config else "") + ".tar.gz"

def config_check_user(config: dict):
    conf_expect_as_user = config.get("expectAsUser", None)
    if conf_expect_as_user is not None and conf_expect_as_user != Consts["CurrentUser"]:
        ask_value: str = ask("incorrect_user", f'This configuration expects you to be user {conf_expect_as_user}, but you are currently user {Consts["CurrentUser"]}. Continue? ', [
            "yes",
            "no",
            "exit"
        ])

        if ask_value != "yes":
            sys.exit(1)

def file_like_pipe(file_from: IO, file_to: IO):
    while True:
        file_bytes = file_from.read(8192)
        if file_bytes:
            while True:
                try:
                    file_to.write(file_bytes)
                    break
                except BlockingIOError:
                    continue
        else:
            break

def command_unpack(opts: dict, rest_argv: List[str]):
    assert len(rest_argv) > 0 and len(rest_argv) < 3, "incorrect argument number in unpack"

    archive_path: str = rest_argv[0]

    selector_entry_prefix: Union[str, None] = None
    selector_entry: Union[str, None] = None

    if len(rest_argv) > 1:
        entry_raw: str = rest_argv[1]
        assert entry_raw != "", "<entry> is empty string"
        if entry_raw[-1] == "/":
            # is entryprefix
            selector_entry_prefix = entry_raw[:-1]
        else:
            selector_entry = entry_raw
    else:
        selector_entry_prefix = ""

    tar, config = read_config_in_archive(archive_path)

    config_check_user(config)

    curr_ver_config: Union[dict, None] = None
    curr_ver_tar: Union[tarfile.TarFile, None] = None

    # check update
    workspace_dir_path = resolve_var_ref_in_dict_by_key(config["commonVarDict"], "WorkspaceDir", "commonVarDict/", None, config)
    workspace_dir_obj = pathlib.Path(workspace_dir_path)
    workspace_conf_obj: pathlib.Path = workspace_dir_obj / "config.yaml"

    if not opts["dry"]:
        workspace_dir_obj.mkdir(mode=0o0700, parents=True, exist_ok=True)
    else:
        print(f'dry: created dir {workspace_dir_obj.as_posix()}')

    workspace_conf_exists = False
    try:
        workspace_conf_exists = workspace_conf_obj.exists()
    except Exception:
        traceback.print_exc()
        ask_value: str = ask("_", f'{workspace_conf_obj.as_posix()} can not be accessed. If you continue, unpacked files will forcibly overwrite files in the system. Continue? ', [
            "yes",
            "no",
            "exit"
       ])

        if ask_value != "yes":
            sys.exit(1)

    if workspace_conf_exists:
        curr_ver_config = read_config_in_path(workspace_conf_obj.as_posix())
        workspace_archive_obj = workspace_dir_obj / make_archive_filename(curr_ver_config)

        workspace_archive_exists = False
        try:
            workspace_archive_exists = workspace_archive_obj.exists()
        except Exception:
            traceback.print_exc()
        
        if not workspace_archive_exists:
            ask_value: str = ask("_", f'{workspace_archive_obj.as_posix()} does not exists or the access is denied. If you continue, unpacked files will forcibly overwrite files in the system. Continue? ', [
                "yes",
                "no",
                "exit"
            ])

            if ask_value != "yes":
                sys.exit(1)

            curr_ver_config = None
        else:
            curr_ver_tar, _ = read_config_in_archive(workspace_archive_obj.as_posix())

    entry: dict
    for entry in config["entries"]:
        if selector_entry:
            if selector_entry != entry["id"]:
                continue

        if selector_entry_prefix:
            if not entry["id"].startswith(selector_entry_prefix):
                continue

        print("\n=======\n" + f'entry: {entry["name"] if "name" in entry else entry["id"]}')
        if entry.get("askForConfirm", False):
            ask_value: str = ask("entry_ask_for_confirm", f'{entry["id"]}: apply this entry? ', [
                "yes",
                "no",
                "exit"
            ])
            if ask_value == "no":
                continue

        if entry["type"] == "command":
            command: str = resolve_var_ref_in_dict_by_key(entry, "command", entry["id"] + "/", entry, config)
            as_user = entry.get("asUser", None)
            bash_command: List

            print(f'running entry command')
            if not opts["dry"]:
                with tempfile.TemporaryDirectory() as tmp_dir_path:
                    tmp_fifo_path = os.path.join(tmp_dir_path, "fifo")
                    if as_user is None or as_user == Consts["CurrentUser"]:
                        bash_command = ["bash", "-i", "-l", tmp_fifo_path]
                    else:
                        bash_command = ["sudo", "-u", as_user, "-i", "bash", "-i", "-l", tmp_fifo_path]

                    os.mkfifo(tmp_fifo_path)
                    proc = subprocess.Popen(bash_command)
                    with contextlib.closing(open(tmp_fifo_path, "w")) as fifo:
                        fifo.write(command)

                    proc_exit_code = proc.wait()
                    if proc_exit_code != 0 and not entry.get("allowFailure", False):
                        raise RuntimeError(f'{" ".join(bash_command)} returned status code {proc_exit_code}')
            else:
                print(f'dry: run command: {command}')
        elif entry["type"] == "file":
            for file in entry.get("files", []):
                print(f'unpacking: {file["name"]}')

                archive_file_path = os.path.join(resolve_var_ref_in_dict_by_key(file, "archiveDir", entry["id"] + "/" + file["name"] + "/", entry, config), file["name"])
                system_file_path = os.path.join(resolve_var_ref_in_dict_by_key(file, "systemDir", entry["id"] + "/" + file["name"] + "/", entry, config), file["name"])
                expect_when_unpack: str = file.get("expectWhenUnpack", "none")

                decided_operation: Union[str, None] = None

                if expect_when_unpack not in ("notExist", "exist", "none"):
                    raise ValueError(f'invalid expectWhenUnpack: {expect_when_unpack}')

                if expect_when_unpack == "notExist":
                    if os.path.exists(system_file_path):
                        ask_value: str = ask("system_file_path_exists_whether_overwrite", f'{system_file_path} exists, which is unexpected. Overwrite? ', [
                            "yes",
                            "no",
                            "all",
                            "nottoall",
                            "exit"
                        ])
                        if ask_value == "no":
                            continue
                        decided_operation = "overwrite"
                elif expect_when_unpack == "exist":
                    if not os.path.exists(system_file_path):
                        ask_value: str = ask("system_file_path_not_exists", f'{system_file_path} does not exist, which is unexpected. Continue? ', [
                            "yes",
                            "no",
                            "all",
                            "exit"
                        ])
                        if ask_value != "yes":
                            sys.exit(1)

                system_file_obj: Union[None, IO] = None
                archive_new_file_obj = tar.extractfile(tar.getmember(archive_file_path))
                archive_new_tempfile = tempfile.NamedTemporaryFile("r+b", delete=False)
                archive_old_tempfile = None
                system_tempfile = None
                file_like_pipe(archive_new_file_obj, archive_new_tempfile)
                overwrite_src_file_path: str = archive_new_tempfile.name

                owner: Union[str, None] = file.get("owner")
                mode: Union[str, None] = file.get("mode")
                if isinstance(mode, int):
                    if mode < 0o000 or mode > 0o777:
                        raise ValueError(f'invalid mode value: {oct(mode)}')

                    mode = oct(mode)[2:]
                
                if os.path.exists(system_file_path):
                    system_file_obj = open(system_file_path, "rb")

                if decided_operation is None and curr_ver_config and archive_file_path in curr_ver_config["entries_dict"].get(entry["id"], {}).get("files_dict", {}) and system_file_obj is not None:
                    # Compare: system, archive-old, archive-new
                    archive_old_tempfile = tempfile.NamedTemporaryFile("r+b", delete=False)
                    system_tempfile = tempfile.NamedTemporaryFile("r+b", delete=False)
                    
                    archive_old_file_obj = curr_ver_tar.extractfile(curr_ver_tar.getmember(archive_file_path))

                    file_like_pipe(archive_old_file_obj, archive_old_tempfile)
                    file_like_pipe(system_file_obj, system_tempfile)

                    archive_old_file_obj.close()

                    archive_old_tempfile.seek(0)
                    archive_new_tempfile.seek(0)
                    system_tempfile.seek(0)

                    file_is_text: bool = True
                    old_equals_system: bool = False
                    archive_old_tempfile_text: Union[io.TextIOWrapper, None] = None
                    system_tempfile_text: Union[io.TextIOWrapper, None] = None
                    try:
                        archive_old_tempfile_text = io.TextIOWrapper(archive_old_tempfile, encoding="utf-8")
                        system_tempfile_text = io.TextIOWrapper(system_tempfile, encoding="utf-8")

                        if archive_old_tempfile_text.read() == system_tempfile_text.read():
                            old_equals_system = True

                        io.TextIOWrapper(archive_new_tempfile, encoding="utf-8").read()
                    except UnicodeDecodeError:
                        file_is_text = False
                        if archive_old_tempfile_text:
                            archive_old_tempfile_text.detach()
                            del archive_old_tempfile_text
                        if system_tempfile_text:
                            system_tempfile_text.detach()
                            del system_tempfile_text

                        archive_old_tempfile.seek(0)
                        system_tempfile.seek(0)
                        if archive_old_tempfile.read() == system_tempfile.read():
                            old_equals_system = True

                    archive_old_tempfile.close()
                    archive_new_tempfile.close()
                    system_tempfile.close()

                    if not old_equals_system and file_is_text:
                        ask_value: str = ask("conflict", f'{system_file_path} is modified since the installation of last version. Overwrite, skip or resolve conflict? ', [
                            "overwrite",
                            "skip",
                            "resolve",
                            "alwaysoverwrite",
                            "alwaysskip",
                            "alwaysresolve",
                            "exit"
                        ])

                        if ask_value == "overwrite":
                            decided_operation = ask_value
                        elif ask_value == "skip":
                            decided_operation = ask_value
                        elif ask_value == "resolve":
                            git_merge_file_command = ["git", "merge-file", "-L", system_file_path + " (system)", "-L", system_file_path + " (null)", "-L", system_file_path + " (new)", system_tempfile.name, "/dev/null", archive_new_tempfile.name]
                            print(f'executing {" ".join(git_merge_file_command)}')
                            proc_exit_code = subprocess.call(git_merge_file_command)

                            if proc_exit_code < 0:
                                raise RuntimeError(f'git merge-file returned status {proc_exit_code}')

                            proc_exit_code = subprocess.call([os.environ.get("EDITOR", "vim"), system_tempfile.name])

                            if proc_exit_code != 0:
                                raise RuntimeError(f'vim returned status {proc_exit_code}')

                            overwrite_src_file_path = system_tempfile.name
                            decided_operation = "overwrite"
                        else:
                            raise RuntimeError(f'unexpected response: {ask_value}')
                    elif not old_equals_system:
                        ask_value: str = ask("conflict_bin", f'{system_file_path} (binary) is modified since the installation of last version. Overwrite or skip? ', [
                            "overwrite",
                            "skip",
                            "alwaysoverwrite",
                            "alwaysskip",
                            "exit"
                        ])

                        if ask_value == "overwrite":
                            decided_operation = ask_value
                        elif ask_value == "skip":
                            decided_operation = ask_value
                        else:
                            raise RuntimeError(f'unexpected response: {ask_value}')
                    else:
                        decided_operation = "overwrite"
                elif decided_operation is None:
                    decided_operation = "overwrite"

                if system_file_obj and not system_file_obj.closed:
                    system_file_obj.close()
                if archive_new_file_obj and not archive_new_file_obj.closed:
                    archive_new_file_obj.close()
                if archive_new_tempfile and not archive_new_tempfile.closed:
                    archive_new_tempfile.close()

                if decided_operation == "overwrite":
                    system_dir_path = pathlib.Path(os.path.abspath(os.path.dirname(system_file_path)))
                    if not opts["dry"]:
                        system_dir_path.mkdir(mode=0o0111 | int(mode, 8), parents=True, exist_ok=True)
                    else:
                        print(f'dry: created dir {system_dir_path.as_posix()}')

                    if not opts["dry"]:
                        os.replace(overwrite_src_file_path, system_file_path)
                    else:
                        print(f'dry: moved {overwrite_src_file_path} to {system_file_path}')
                elif decided_operation == "skip":
                    pass
                else:
                    raise RuntimeError(f'unexpected decided_operation: {decided_operation}')

                try:
                    os.unlink(archive_old_tempfile)
                except Exception:
                    pass

                try:
                    os.unlink(archive_new_tempfile)
                except Exception:
                    pass

                try:
                    os.unlink(system_tempfile)
                except Exception:
                    pass

                if owner is not None:
                    if not opts["dry"]:
                        proc_exit_code = subprocess.call(["chown", owner, system_file_path])
                        if proc_exit_code != 0:
                            raise RuntimeError(f'chown returned status {proc_exit_code}')
                    else:
                        print(f'dry: chowned {system_file_path} to {owner}')
                
                if mode is not None:
                    if not opts["dry"]:
                        proc_exit_code = subprocess.call(["chmod", mode, system_file_path])
                        if proc_exit_code != 0:
                            raise RuntimeError(f'chmod returned status {proc_exit_code}')
                    else:
                        print(f'dry: chmoded {system_file_path} to {mode}')

    if curr_ver_tar:
        curr_ver_tar.close()

    print("\n=======\nfinished\n=======")

    dbg_print(f'extracting tar to {workspace_dir_path}...')

    if not opts["dry"]:
        tar.extractall(workspace_dir_path, members=[
            ti for ti in tar.getmembers()
            if ti.name.startswith(Consts["ExtraArchiveFilePrefix"])
        ])
        tar.extract("config.yaml", workspace_dir_path)
    else:
        print(f'dry: extracted {Consts["ExtraArchiveFilePrefix"]} to workspace: {workspace_dir_path}')
        print(f'dry: extracted config.yaml to workspace: {workspace_dir_path}')
    
    tar.close()

    if os.path.normpath(os.path.abspath(workspace_dir_path)) == os.path.normpath(os.path.abspath(os.path.dirname(archive_path))):
        print("skipping tar copying")
    else:
        if not opts["dry"]:
            shutil.copy2(archive_path, workspace_dir_path)
        else:
            print(f'dry: copied {archive_path} to workspace: {workspace_dir_path}')

def command_pack(opts: dict, rest_argv: List[str]):
    assert len(rest_argv) == 0, "incorrect argument number in pack"
    
    config: dict = read_config_in_path("./config.yaml")

    if opts["dry"]:
        raise RuntimeError("--dry option is invalid when packing")

    config_check_user(config)

    tar = tarfile.open(make_archive_filename(config), "w:gz")

    entry: dict
    for entry in config["entries"]:
        if entry["type"] != "file":
            continue

        for file in entry.get("files", []):
            if file.get("fromExtraArchiveDir", False):
                continue

            archive_file_path = os.path.join(resolve_var_ref_in_dict_by_key(file, "archiveDir", entry["id"] + "/" + file["name"] + "/", entry, config), file["name"])
            system_file_path = os.path.join(resolve_var_ref_in_dict_by_key(file, "systemDir", entry["id"] + "/" + file["name"] + "/", entry, config), file["name"])
            system_file = open(system_file_path, "rb")

            new_member = tar.gettarinfo(system_file_path, archive_file_path, system_file)

            mode: Union[str, None] = file.get("mode", None)
            if mode is not None:
                new_member.mode = int(file.get("mode", "0640"), 8)

            owner: Union[str, None] = file.get("owner", None)
            if owner is not None:
                owner = owner.split(":")
                assert len(owner) == 2, f'{owner} invalid!'
                new_member.uid, new_member.gid, new_member.uname, new_member.gname = pwd.getpwnam(owner[0]).pw_uid, grp.getgrnam(owner[1]).gr_gid, owner[0], owner[1]

            print(f'adding: {system_file_path} -> {archive_file_path}')
            tar.addfile(new_member, system_file)
    
    extra_archive_dir = resolve_var_ref("ExtraArchiveFilePrefix", { "refVar": "ExtraArchiveFilePrefix" }, None, config)
    if os.path.isdir(extra_archive_dir):
        print(f'adding: {extra_archive_dir}')
        tar.add(extra_archive_dir, extra_archive_dir, True)
    else:
        warn_print(f'WARNING: extra archive dir {extra_archive_dir} does not exist. Not packing.')

    print(f'adding: ./config.yaml -> config.yaml')
    tar.add("./config.yaml", "config.yaml", False)

    tar.close()
    
    
COMMAND_FUNC_ENTRIES = [
    CommandFuncEntry(("unpack", "u"), command_unpack),
    CommandFuncEntry(("pack", "p"), command_pack)
]

def init():
    Consts["CurrentUserId"] = os.geteuid()
    Consts["CurrentUser"] = pwd.getpwuid(Consts["CurrentUserId"])[0]
    Consts["CurrentGroupId"] = os.getegid()
    Consts["CurrentGroup"] = grp.getgrgid(Consts["CurrentGroupId"])[0]

def main():
    init()
    opts_raw, args = getopt.getopt(sys.argv[1:], "-d", ["dry"])
    opts = {
        "dry": False
    }

    for opt_raw in opts_raw:
        if opt_raw[0] == "-d" or opt_raw[0] == "--dry":
            opts["dry"] = True

    cfe: CommandFuncEntry
    for cfe in COMMAND_FUNC_ENTRIES:
        if args[0] in cfe.command_names:
            cfe.func(opts, args[1:])

main()
