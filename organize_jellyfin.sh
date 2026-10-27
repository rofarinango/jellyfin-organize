#!/usr/bin/env bash
set -euo pipefail

SSH_HOST="jellyfin-pi"
SOURCE_DIR="$HOME/Documents/Jellyfin Content"
REMOTE_MOVIES="/mnt/external/StreamingContent/Movies"
REMOTE_SHOWS="/mnt/external/StreamingContent/Shows"
REMOTE_OWNER="jellyfin:jellyfin"

usage() {
  cat <<EOF
Organiza archivos de "$SOURCE_DIR" en la librería de Jellyfin de la Raspberry Pi.

Uso:
  Película:
    $(basename "$0") movie "<archivo>" "<Título>" <Año>

  Serie:
    $(basename "$0") show "<archivo>" "<Título de la serie>" <Año> <Temporada> <Episodio>

Ejemplos:
  $(basename "$0") movie "Dune.Part.Two.2024.mkv" "Dune: Part Two" 2024
  $(basename "$0") show "Bleach TYBW S01E49.mp4" "Bleach" 2004 2 49

Notas:
  - El <archivo> debe estar dentro de "$SOURCE_DIR" (pasa solo el nombre del archivo).
  - <Año>, <Temporada> y <Episodio> determinan el nombre siguiendo la convención de Jellyfin.
    Para series, usa el año de la TEMPORADA/entrada en TMDB que corresponda, no necesariamente
    el año de estreno original de la franquicia (ej. secuelas que TMDB cataloga como la misma
    serie usan el año de la serie "padre"; verifica en themoviedb.org si tienes dudas).
  - El script NO borra el archivo original en tu Mac. Bórralo tú manualmente una vez
    confirmes que se ve bien en Jellyfin.
EOF
}

die() { echo "Error: $*" >&2; exit 1; }

pad2() { printf "%02d" "$1"; }

transfer() {
  local src="$1" remote_dir="$2" remote_filename="$3"
  local tmp_remote="/tmp/$(uuidgen).tmp"
  echo "TMP_REMOTE:$tmp_remote"

  local local_md5
  local_md5=$(md5 -q "$src")
  echo "MD5 local: $local_md5"

  echo "Creando carpeta remota: $remote_dir"
  ssh "$SSH_HOST" "sudo mkdir -p '$remote_dir'"

  echo "Transfiriendo archivo..."
  scp "$src" "$SSH_HOST:$tmp_remote"

  echo "Moviendo a destino final y ajustando permisos..."
  ssh "$SSH_HOST" "sudo mv '$tmp_remote' '$remote_dir/$remote_filename' && sudo chown $REMOTE_OWNER '$remote_dir/$remote_filename' && sudo chmod 644 '$remote_dir/$remote_filename'"

  local remote_md5
  remote_md5=$(ssh "$SSH_HOST" "md5sum '$remote_dir/$remote_filename'" | awk '{print $1}')
  echo "MD5 remoto: $remote_md5"

  if [[ "$local_md5" != "$remote_md5" ]]; then
    die "los checksums no coinciden, revisa la transferencia antes de borrar el original."
  fi

  echo
  echo "OK: '$remote_filename' copiado correctamente a:"
  echo "  $remote_dir"
  echo "El original en tu Mac NO fue borrado: $src"
}

cmd_movie() {
  local file="$1" title="$2" year="$3"
  local src="$SOURCE_DIR/$file"
  [[ -f "$src" ]] || die "no encontré '$src'"

  local ext="${file##*.}"
  local folder_name="$title ($year)"
  local remote_dir="$REMOTE_MOVIES/$folder_name"
  local remote_filename="$folder_name.$ext"

  transfer "$src" "$remote_dir" "$remote_filename"
}

cmd_show() {
  local file="$1" title="$2" year="$3" season="$4" episode="$5"
  local src="$SOURCE_DIR/$file"
  [[ -f "$src" ]] || die "no encontré '$src'"

  local ext="${file##*.}"
  local season_padded episode_padded
  season_padded=$(pad2 "$season")
  episode_padded=$(pad2 "$episode")

  local folder_name="$title ($year)"
  local remote_dir="$REMOTE_SHOWS/$folder_name/Season $season_padded"
  local remote_filename="$title S${season_padded}E${episode_padded}.$ext"

  transfer "$src" "$remote_dir" "$remote_filename"
}

main() {
  [[ $# -lt 1 ]] && { usage; exit 1; }
  local kind="$1"; shift

  case "$kind" in
    movie)
      [[ $# -eq 3 ]] || { usage; exit 1; }
      cmd_movie "$@"
      ;;
    show)
      [[ $# -eq 5 ]] || { usage; exit 1; }
      cmd_show "$@"
      ;;
    -h|--help|help)
      usage
      ;;
    *)
      usage
      exit 1
      ;;
  esac
}

main "$@"
