Ce dossier peut contenir des binaires locaux integres a l'application.

L'application cherche d'abord ici :

- `tools/bin/edi2eti`
- `tools/bin/odr-edi2edi`
- `tools/bin/eti2zmq`

avant de chercher dans le `PATH`.

Pour remplir ce dossier a partir des outils deja installes sur la machine :

```bash
./scripts/integrate-mmbtools.sh
```
