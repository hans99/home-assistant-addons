#!/usr/bin/awk -f

BEGIN {
  found_section = 0;
  found_key = 0;
  edited_row = 0;
}

$0 ~ "^" key "=" {
  if (found_section) {
    found_key = 1;
    edited_row = 1;
    if (value || value == 0) {
      print key "=" value;
    }
  }
}

$0 ~ "^\\[" {
  if (found_section && !found_key) {
    found_key = 1;
    if (value || value == 0) {
      print key "=" value;
    }
  }
}

$0 ~ "^\\[" section "\\]" {
  found_section = 1;
}

/.*/ {
  if(!edited_row) {
    print $0;
  }
  edited_row = 0;
}

END {
  if (!found_section) {
    print "[" section "]";
    print key "=" value;
  }
}
