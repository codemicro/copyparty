#!/usr/bin/env python
# coding: utf-8
from __future__ import print_function, unicode_literals

import os
import stat
import time
import json
from datetime import datetime
import calendar
import mimetypes
import cgi

from .__init__ import E, PY2
from .util import *  # noqa  # pylint: disable=unused-wildcard-import

if not PY2:
    unicode = str


class HttpCli(object):
    """
    Spawned by HttpConn to process one http transaction
    """

    def __init__(self, conn):
        self.conn = conn
        self.s = conn.s
        self.sr = conn.sr
        self.addr = conn.addr
        self.args = conn.args
        self.auth = conn.auth
        self.log_func = conn.log_func
        self.log_src = conn.log_src

        self.bufsz = 1024 * 32
        self.absolute_urls = False
        self.out_headers = {}

    def log(self, msg):
        self.log_func(self.log_src, msg)

    def run(self):
        try:
            headerlines = read_header(self.sr)
            if not headerlines:
                return False

            try:
                self.mode, self.req, _ = headerlines[0].split(" ")
            except:
                raise Pebkac("bad headers:\n" + "\n".join(headerlines))

        except Pebkac as ex:
            self.loud_reply(str(ex))
            return False

        self.headers = {}
        for header_line in headerlines[1:]:
            k, v = header_line.split(":", 1)
            self.headers[k.lower()] = v.strip()

        self.uname = "*"
        if "cookie" in self.headers:
            cookies = self.headers["cookie"].split(";")
            for k, v in [x.split("=", 1) for x in cookies]:
                if k != "cppwd":
                    continue

                v = unescape_cookie(v)
                if v in self.auth.iuser:
                    self.uname = self.auth.iuser[v]

                break

        if self.uname:
            self.rvol = self.auth.vfs.user_tree(self.uname, readable=True)
            self.wvol = self.auth.vfs.user_tree(self.uname, writable=True)

        # split req into vpath + uparam
        uparam = {}
        if "?" not in self.req:
            if not self.req.endswith("/"):
                self.absolute_urls = True

            vpath = undot(self.req)
        else:
            vpath, arglist = self.req.split("?", 1)
            if not vpath.endswith("/"):
                self.absolute_urls = True

            vpath = undot(vpath)
            for k in arglist.split("&"):
                if "=" in k:
                    k, v = k.split("=", 1)
                    uparam[k.lower()] = v.strip()
                else:
                    uparam[k.lower()] = True

        self.uparam = uparam
        self.vpath = unquotep(vpath)

        try:
            if self.mode in ["GET", "HEAD"]:
                return self.handle_get()
            elif self.mode == "POST":
                return self.handle_post()
            else:
                raise Pebkac('invalid HTTP mode "{0}"'.format(self.mode))

        except Pebkac as ex:
            try:
                self.loud_reply(str(ex))
            except Pebkac:
                pass

            return False

    def reply(self, body, status="200 OK", mime="text/html", headers=[]):
        # TODO something to reply with user-supplied values safely
        response = [
            "HTTP/1.1 " + status,
            "Connection: Keep-Alive",
            "Content-Type: " + mime,
            "Content-Length: " + str(len(body)),
        ]
        for k, v in self.out_headers.items():
            response.append("{}: {}".format(k, v))

        response.extend(headers)
        response_str = "\r\n".join(response).encode("utf-8")
        try:
            self.s.sendall(response_str + b"\r\n\r\n" + body)
        except:
            raise Pebkac("client disconnected before http response")

        return body

    def loud_reply(self, body, *args, **kwargs):
        self.log(body.rstrip())
        self.reply(b"<pre>" + body.encode("utf-8"), *list(args), **kwargs)

    def handle_get(self):
        logmsg = "{:4} {}".format(self.mode, self.req)

        if "range" in self.headers:
            try:
                rval = self.headers["range"].split("=", 1)[1]
            except:
                rval += self.headers["range"]

            logmsg += " [\033[36m" + rval + "\033[0m]"

        self.log(logmsg)

        # "embedded" resources
        if self.vpath.startswith(".cpr"):
            static_path = os.path.join(E.mod, "web/", self.vpath[5:])

            if os.path.isfile(static_path):
                return self.tx_file(static_path)

        # conditional redirect to single volumes
        if self.vpath == "" and not self.uparam:
            nread = len(self.rvol)
            nwrite = len(self.wvol)
            if nread + nwrite == 1 or (self.rvol == self.wvol and nread == 1):
                if nread == 1:
                    self.vpath = self.rvol[0]
                else:
                    self.vpath = self.wvol[0]

                self.absolute_urls = True

        # go home if verboten
        self.readable, self.writable = self.conn.auth.vfs.can_access(
            self.vpath, self.uname
        )
        if not self.readable and not self.writable:
            self.log("inaccessible: {}".format(self.vpath))
            self.uparam = {"h": True}

        if "h" in self.uparam:
            self.vpath = None
            return self.tx_mounts()

        if self.readable:
            return self.tx_browser()
        else:
            return self.tx_upper()

    def handle_post(self):
        self.log("POST " + self.req)

        try:
            if self.headers["expect"].lower() == "100-continue":
                self.s.sendall(b"HTTP/1.1 100 Continue\r\n\r\n")
        except KeyError:
            pass

        if 'content-type' not in self.headers:
            raise Pebkac("you can't post without a content-type header")

        ctype = self.headers['content-type'].lower()

        if 'multipart/form-data' in ctype:
            return self.handle_post_multipart()

        if 'text/plain' in ctype:
            return self.handle_post_json()

        if 'application/octet-stream' in ctype:
            return self.handle_post_binary()

        raise Pebkac("don't know how to handle a {} POST".format(ctype))

    def handle_post_multipart(self):
        self.parser = MultipartParser(self.log, self.sr, self.headers)
        self.parser.parse()

        act = self.parser.require("act", 64)

        if act == "bput":
            return self.handle_plain_upload()

        if act == "login":
            return self.handle_login()

        raise Pebkac('invalid action "{}"'.format(act))

    def handle_post_json(self):
        if 'content-length' not in self.headers:
            raise Pebkac('you must supply a content-length for JSON POST')

        remains = int(self.headers['content-length'])
        if remains > 1024 * 1024:
            raise Pebkac('json 2big')

        enc = 'utf-8'
        ctype = ctype = self.headers['content-type'].lower()
        if 'charset' in ctype:
            enc = ctype.split('charset')[1].strip(' =').split(';')[0].strip()

        json_buf = b''
        while len(json_buf) < remains:
            json_buf += self.sr.recv(32 * 1024)

        self.log('decoding {} bytes of {} json'.format(len(json_buf), enc))
        try:
            body = json.loads(json_buf.decode(enc, 'replace'))
        except:
            raise Pebkac("you POSTed invalid json")

        print(body)

    def handle_post_binary(self):
        raise Exception('todo')

    def handle_login(self):
        pwd = self.parser.require("cppwd", 64)
        self.parser.drop()

        if pwd in self.auth.iuser:
            msg = "login ok"
        else:
            msg = "naw dude"
            pwd = "x"  # nosec

        h = ["Set-Cookie: cppwd={}; Path=/".format(pwd)]
        html = self.conn.tpl_msg.render(h1=msg, h2='<a href="/">ack</a>', redir="/")
        self.reply(html.encode("utf-8"), headers=h)
        return True

    def handle_plain_upload(self):
        nullwrite = self.args.nw
        vfs, rem = self.conn.auth.vfs.get(self.vpath, self.uname, False, True)

        # rem is escaped at this point,
        # this is just a sanity check to prevent any disasters
        if rem.startswith("/") or rem.startswith("../") or "/../" in rem:
            raise Exception("that was close")

        files = []
        errmsg = ""
        t0 = time.time()
        try:
            for nfile, (p_field, p_file, p_data) in enumerate(self.parser.gen):
                if not p_file:
                    self.log("discarding incoming file without filename")
                    # fallthrough

                fn = os.devnull
                if p_file and not nullwrite:
                    fdir = os.path.join(vfs.realpath, rem)
                    fn = os.path.join(fdir, sanitize_fn(p_file))

                    if not os.path.isdir(fsenc(fdir)):
                        raise Pebkac("that folder does not exist")

                    # TODO broker which avoid this race
                    # and provides a new filename if taken
                    if os.path.exists(fsenc(fn)):
                        fn += ".{:.6f}".format(time.time())

                try:
                    with open(fsenc(fn), "wb") as f:
                        self.log("writing to {0}".format(fn))
                        sz, sha512 = hashcopy(self.conn, p_data, f)
                        if sz == 0:
                            raise Pebkac("empty files in post")

                        files.append([sz, sha512])

                except Pebkac:
                    if not nullwrite:
                        os.rename(fsenc(fn), fsenc(fn + ".PARTIAL"))

                    raise

        except Pebkac as ex:
            errmsg = str(ex)

        td = time.time() - t0
        sz_total = sum(x[0] for x in files)
        spd = (sz_total / td) / (1024 * 1024)

        status = "OK"
        if errmsg:
            self.log(errmsg)
            errmsg = "ERROR: " + errmsg
            status = "ERROR"

        msg = "{0} // {1} bytes // {2:.3f} MiB/s\n".format(status, sz_total, spd)

        for sz, sha512 in files:
            msg += "sha512: {0} // {1} bytes\n".format(sha512[:56], sz)
            # truncated SHA-512 prevents length extension attacks;
            # using SHA-512/224, optionally SHA-512/256 = :64

        self.log(msg)
        if not nullwrite:
            # TODO this is bad
            log_fn = "up.{:.6f}.txt".format(t0)
            with open(log_fn, "wb") as f:
                f.write(
                    (
                        "\n".join(
                            unicode(x)
                            for x in [
                                ":".join(unicode(x) for x in self.addr),
                                msg.rstrip(),
                            ]
                        )
                        + "\n"
                        + errmsg
                        + "\n"
                    ).encode("utf-8")
                )

        html = self.conn.tpl_msg.render(
            h2='<a href="/{}">return to /{}</a>'.format(
                quotep(self.vpath), cgi.escape(self.vpath, quote=True)
            ),
            pre=msg,
        )
        self.reply(html.encode("utf-8"))
        self.parser.drop()
        return True

    def tx_file(self, path):
        file_ts = os.path.getmtime(fsenc(path))
        file_dt = datetime.utcfromtimestamp(file_ts)
        file_lastmod = file_dt.strftime("%a, %b %d %Y %H:%M:%S GMT")

        do_send = True
        status = "200 OK"
        extra_headers = []
        logmsg = "{:4} {} {}".format("", self.req, status)

        #
        # if-modified

        if "if-modified-since" in self.headers:
            cli_lastmod = self.headers["if-modified-since"]
            try:
                cli_dt = time.strptime(cli_lastmod, "%a, %b %d %Y %H:%M:%S GMT")
                cli_ts = calendar.timegm(cli_dt)
                do_send = int(file_ts) > int(cli_ts)
            except:
                self.log("bad lastmod format: {}".format(cli_lastmod))
                do_send = file_lastmod != cli_lastmod

        if not do_send:
            status = "304 Not Modified"

        #
        # partial

        file_sz = os.path.getsize(fsenc(path))
        lower = 0
        upper = file_sz

        if do_send and "range" in self.headers:
            try:
                hrange = self.headers["range"]
                a, b = hrange.split("=", 1)[1].split("-")

                if a.strip():
                    lower = int(a.strip())
                else:
                    lower = 0

                if b.strip():
                    upper = int(b.strip()) + 1
                else:
                    upper = file_sz

                if lower < 0 or lower >= file_sz or upper < 0 or upper > file_sz:
                    raise Pebkac("na")

            except:
                self.loud_reply("invalid range requested: " + hrange)

            status = "206 Partial Content"
            extra_headers.append(
                "Content-Range: bytes {}-{}/{}".format(lower, upper - 1, file_sz)
            )

            logmsg += " [\033[36m" + str(lower) + "-" + str(upper) + "\033[0m]"

        #
        # send reply

        mime = mimetypes.guess_type(path)[0] or "application/octet-stream"

        headers = [
            "HTTP/1.1 " + status,
            "Connection: Keep-Alive",
            "Content-Type: " + mime,
            "Content-Length: " + str(upper - lower),
            "Accept-Ranges: bytes",
            "Last-Modified: " + file_lastmod,
        ]
        headers.extend(extra_headers)
        headers = "\r\n".join(headers).encode("utf-8") + b"\r\n\r\n"
        self.s.sendall(headers)

        if self.mode == "HEAD" or not do_send:
            self.log(logmsg)
            return True

        # 512 kB is optimal for huge files, use 64k
        with open(fsenc(path), "rb", 64 * 1024) as f:
            remains = upper - lower
            f.seek(lower)
            while remains > 0:
                # time.sleep(0.01)
                buf = f.read(4096)
                if not buf:
                    break

                if remains < len(buf):
                    buf = buf[:remains]

                remains -= len(buf)

                try:
                    self.s.sendall(buf)
                except:
                    logmsg += " \033[31m" + str(upper - remains) + "\033[0m"
                    self.log(logmsg)
                    return False

        self.log(logmsg)
        return True

    def tx_mounts(self):
        html = self.conn.tpl_mounts.render(this=self)
        self.reply(html.encode("utf-8"))
        return True

    def tx_upper(self):
        # return html for basic uploader;
        # js rewrites to up2k unless uparam['b']
        self.loud_reply("TODO jupper {}".format(self.vpath))
        return True

    def tx_browser(self):
        vpath = ""
        vpnodes = [["", "/"]]
        for node in self.vpath.split("/"):
            if not vpath:
                vpath = node
            else:
                vpath += "/" + node

            vpnodes.append([quotep(vpath) + "/", cgi.escape(node)])

        vn, rem = self.auth.vfs.get(self.vpath, self.uname, True, False)
        abspath = vn.canonical(rem)

        if not os.path.exists(fsenc(abspath)):
            print(abspath)
            raise Pebkac("404 not found")

        if not os.path.isdir(fsenc(abspath)):
            return self.tx_file(abspath)

        fsroot, vfs_ls, vfs_virt = vn.ls(rem, self.uname)
        vfs_ls.extend(vfs_virt)

        dirs = []
        files = []
        for fn in vfs_ls:
            href = fn
            if self.absolute_urls:
                href = vpath + "/" + fn

            fspath = fsroot + "/" + fn
            inf = os.stat(fsenc(fspath))

            is_dir = stat.S_ISDIR(inf.st_mode)
            if is_dir:
                margin = "DIR"
                href += "/"
            else:
                margin = "-"

            sz = inf.st_size
            dt = datetime.utcfromtimestamp(inf.st_mtime)
            dt = dt.strftime("%Y-%m-%d %H:%M:%S")

            item = [margin, quotep(href), cgi.escape(fn, quote=True), sz, dt]
            if is_dir:
                dirs.append(item)
            else:
                files.append(item)

        ts = ""
        # ts = "?{}".format(time.time())

        dirs.extend(files)
        html = self.conn.tpl_browser.render(
            vdir=self.vpath,
            vpnodes=vpnodes,
            files=dirs,
            can_upload=self.writable,
            ts=ts,
        )
        self.reply(html.encode("utf-8", "replace"))
        return True
