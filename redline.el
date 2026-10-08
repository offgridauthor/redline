;;; redline.el --- Word-style review in org, with .docx round trips  -*- lexical-binding: t; -*-

;; Copyright (C) 2026 Stephen Lloyd Webber

;; Author: Stephen Lloyd Webber <offgridauthor@gmail.com>
;; Maintainer: Stephen Lloyd Webber <offgridauthor@gmail.com>
;; URL: https://github.com/offgridauthor/redline
;; Version: 0.1.0
;; Package-Requires: ((emacs "29.1") (cm-mode "1.10"))
;; Keywords: wp, text, convenience
;; SPDX-License-Identifier: GPL-3.0-or-later

;; This file is not part of GNU Emacs.

;; This program is free software: you can redistribute it and/or modify
;; it under the terms of the GNU General Public License as published by
;; the Free Software Foundation, either version 3 of the License, or
;; (at your option) any later version.

;; This program is distributed in the hope that it will be useful,
;; but WITHOUT ANY WARRANTY; without even the implied warranty of
;; MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
;; GNU General Public License for more details.

;; You should have received a copy of the GNU General Public License
;; along with this program.  If not, see <https://www.gnu.org/licenses/>.

;;; Commentary:

;; Redline lets you review a Word manuscript in org the way you would in
;; Word: tracked changes, comment threads, replies, accept and reject.
;;
;; - `redline-import' turns a .docx into an org file beside it.  The
;;   client's tracked changes and comments come in as CriticMarkup
;;   ({++added++}, {--deleted--}, {~~old~>new~~}, {==text==}{>>comment<<}),
;;   which cm-mode edits.
;; - `redline-mode' adds tracking that's on by default in files from a
;;   .docx, a review pane, author tags in the margin, and a clean view.
;; - `redline-export' writes your edits into a copy of the original .docx
;;   as real Word tracked changes and comment threads.  Paragraphs you
;;   didn't touch are copied byte for byte; edited ones keep the client's
;;   fonts and styles character by character.  The original is only read.
;;
;; The .docx work is done by redline.py, which comes with this package
;; and needs Python 3 with lxml (Debian/Ubuntu: python3-lxml).
;;
;; Typical setup:
;;
;;   (add-hook 'org-mode-hook #'redline-maybe-enable)
;;   (keymap-global-set "C-c * o" #'redline-import)

;;; Code:

(require 'cm-mode)
(require 'seq)
(require 'subr-x)
(require 'redline-pane)
(require 'ol)

(defgroup redline nil
  "Word-style review in org, with .docx round trips."
  :group 'wp
  :prefix "redline-")

(defcustom redline-script
  (expand-file-name "redline.py"
                    (file-name-directory (or load-file-name buffer-file-name
                                             (locate-library "redline"))))
  "Path to redline.py, the .docx converter that comes with this package."
  :type 'file)

(defcustom redline-python "python3"
  "Python 3 interpreter with lxml installed."
  :type 'string)

(defcustom redline-my-name nil
  "Your name as Word should show it on your changes and comments.
Nil means the value of the variable `user-full-name'."
  :type '(choice (const :tag "user-full-name" nil) string))

(defcustom redline-my-other-names nil
  "Names Word has recorded for you in earlier rounds, like \"Stephen W\".
Word labels every change and comment with the name it was made
under, and that name can drift between computers and versions of
Word.  Changes and comments under these names come in as yours,
with your tag, so you can revise, reply to, accept, or reject them
as your own work.  Each still goes back to Word under the name it
was made with."
  :type '(repeat string))

(defcustom redline-track-changes-on-open t
  "Non-nil turns on change tracking when a file made from a .docx opens.
Client files are where every edit should show, the way Word's Track
Changes would be on."
  :type 'boolean)

(defcustom redline-style-cm-faces t
  "Non-nil gives cm-mode's faces colors for light and dark themes."
  :type 'boolean)

;;;; Running redline.py

(defun redline--me ()
  "Your tag and name, as redline.py takes them."
  (format "%s=%s" (or cm-author "me")
          (or redline-my-name
              (and (not (string-empty-p user-full-name)) user-full-name)
              cm-author "me")))

(defun redline--alias-args ()
  "Arguments that tell redline.py your other names."
  (mapcan (lambda (n) (list "--alias" n)) redline-my-other-names))

(defun redline--run (&rest args)
  "Run redline.py with ARGS; return its output, or signal an error."
  (unless (file-exists-p redline-script)
    (user-error "Can't find redline.py at %s" redline-script))
  (let ((buf (get-buffer-create "*redline*")))
    (with-current-buffer buf (erase-buffer))
    (let* ((default-directory temporary-file-directory) ; paths passed are absolute
           (status (apply #'call-process redline-python nil buf nil
                          redline-script (delq nil args))))
      (with-current-buffer buf
        (unless (eql status 0)
          (display-buffer buf)
          (error "Redline: the converter stopped; see *redline*"))
        (goto-char (point-min))
        (when (re-search-forward "^note: " nil t)
          (display-buffer buf))
        (string-trim (buffer-string))))))

;;;; Import and export

;;;###autoload
(defun redline-import (docx)
  "Make an org file from DOCX, beside it, and visit it.
An existing org file of the same name is opened as it is, never
replaced."
  (interactive
   (list (read-file-name "Word file to open in org: " nil nil t nil
                         (lambda (f) (or (file-directory-p f)
                                         (string-match-p "\\.docx\\'" f))))))
  (let* ((docx (expand-file-name docx))
         (org (concat (file-name-sans-extension docx) ".org")))
    (when (file-exists-p org)
      (if (y-or-n-p (format "%s exists.  Open it as it is? " (file-name-nondirectory org)))
          (setq docx nil)
        (user-error "Left %s alone; rename it for a fresh import"
                    (file-name-nondirectory org))))
    (when docx
      (apply #'redline--run "import" docx "-o" org "--me" (redline--me)
             (redline--alias-args)))
    (find-file org)))

(defun redline--fresh-name (stem)
  "A new .docx name from STEM, your tag, and today's date."
  (let* ((base (format "%s-%s-%s" stem (or cm-author "edits")
                       (format-time-string "%Y-%m-%d")))
         (name (concat base ".docx"))
         (n 2))
    (while (file-exists-p name)
      (setq name (format "%s-%d.docx" base n)
            n (1+ n)))
    name))

(defun redline-docx-file-p ()
  "Non-nil if this buffer came from a .docx (it has #+review_source:)."
  (save-excursion
    (goto-char (point-min))
    (re-search-forward "^#\\+review_source:" 4000 t)))

(defun redline-export ()
  "Export your edits and comments into a copy of this file's .docx.
The copy goes beside the org file as NAME-TAG-DATE.docx; nothing is
written over."
  (interactive)
  (unless (redline-docx-file-p)
    (user-error "No #+review_source: line; this file didn't come from a .docx"))
  (save-buffer)
  (let ((out (redline--fresh-name (file-name-sans-extension buffer-file-name))))
    (redline--run "export" buffer-file-name "-o" out "--me" (redline--me))
    (message "Wrote %s" (file-name-nondirectory out))))

;;;; The minor mode

(defvar-keymap redline-mode-map
  :doc "Keys for `redline-mode', beside cm-mode's own under C-c *."
  "C-c * l" #'redline-pane
  "C-c * h" #'redline-toggle-clean-view
  "C-c * r" #'redline-reply
  "C-c * k" #'redline-delete-comment
  "C-c * o" #'redline-import
  "C-c * w" #'redline-export
  "<right-margin> <mouse-1>" #'redline-margin-click)

;;;###autoload
(define-minor-mode redline-mode
  "Review this buffer the way Word does.
Tracked changes, comment threads, a review pane, margin tags, and
.docx export.

\\{redline-mode-map}"
  :lighter " Redline"
  :keymap redline-mode-map
  (if redline-mode
      (progn
        (unless cm-mode (cm-mode 1))
        (when redline-style-cm-faces (redline-apply-faces))
        ;; "<<}{>>" between two comments reads to org as a <<target>>.
        (face-remap-add-relative 'org-target '(:underline nil))
        (redline-pane-setup)
        (when (and redline-track-changes-on-open
                   (redline-docx-file-p)
                   (not cm-follow-changes-mode))
          (cm-follow-changes-mode 1)))
    (redline-pane-teardown)))

(defconst redline--markup-re
  "^#\\+review_source:\\|{>>\\|{\\+\\+\\|{--\\|{~~\\|{=="
  "Signs that a buffer contains review markup.")

;;;###autoload
(defun redline-maybe-enable ()
  "Turn on `redline-mode' if this buffer came from a .docx or has CriticMarkup.
Meant for `org-mode-hook', so ordinary org files stay as they are."
  (when (save-excursion
          (goto-char (point-min))
          (re-search-forward redline--markup-re nil t))
    (redline-mode 1)))

;; [[docx:12.0][...]] marks a link, field, image, or note kept from the
;; Word file.  Following it says what it is; its text can't be edited.
(org-link-set-parameters
 "docx"
 :follow (lambda (_path _arg)
           (message "%s"
                    (concat "Kept from the Word file as it was (a link, field, image, "
                            "or note).  Delete it whole if it should go; its text "
                            "can't be edited here.")))
 :help-echo "Kept from the Word file; goes back out unchanged")

(provide 'redline)
;;; redline.el ends here
