# Third-Party Notices

`dabstream2easydab` can use or redistribute the following third-party tools,
depending on the installation or packaging method.

## Bundled or Invoked Tools

### ODR-EDI2EDI

- Upstream: <https://github.com/Opendigitalradio/ODR-EDI2EDI>
- Role in this project: `EDI/TCP -> EDI/UDP` bridge before `edi2eti`
- Upstream license information: the upstream repository exposes `COPYING` and
  labels the project with a `GPL-3.0` license on GitHub

### eti-tools

- Upstream: <https://github.com/piratfm/eti-tools>
- Tools used here: `edi2eti`, `eti2zmq`
- Role in this project:
  - `edi2eti`: `EDI -> ETI`
  - `eti2zmq`: `ETI -> ZeroMQ`
- Upstream license information: the upstream repository exposes `LICENSE` and
  labels the project with an `MPL-2.0` license on GitHub

## Practical Notes

- The source repository of `dabstream2easydab` does not need to include the
  source code of those projects as long as it only references or downloads them.
- If you publish binary packages that bundle those executables, keep the
  relevant upstream license texts and attribution notices with your release.
- Those third-party tools remain under their own licenses; this repository
  license applies to the `dabstream2easydab` code itself.
