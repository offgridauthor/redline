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

(defcustom redline-word-tracking-on t
  "Non-nil switches on Track Changes in the .docx you send back.
The author's answers to your edits then show as tracked changes too.
Nil leaves the switch as the original had it."
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
    (redline--run "export" buffer-file-name "-o" out "--me" (redline--me)
                  (unless redline-word-tracking-on "--keep-settings"))
    (message "Wrote %s" (file-name-nondirectory out))))

;;;; Kept objects: one piece each

;; A link, field, footnote mark, or page break kept from Word shows as
;; [[docx:12.0][text]], and a table as a "#+docx_block: 39 tbl" line.
;; Their text can't change anything in Word, so it's read-only here;
;; deleting, killing, or moving one takes the whole piece (tracked, when
;; tracking is on), and export deletes, inserts, or moves the original.

(defconst redline--unit-re
  (concat "\\[\\[docx:[0-9]+\\.[0-9]+\\]\\(?:\\[[^]]*\\]\\)?\\]"
          "\\|^\\(?:{\\(?:--\\|\\+\\+\\)\\)?\\(#\\+docx_block:[^{}\n]*\\)")
  "A kept object, or (group 1) the text of a kept block's line.")

(defconst redline--unit-message
  "Kept from Word: delete or move it whole (its text can't be edited)"
  "Shown when typing inside a kept object.")

(defun redline--fontify-units (limit)
  "Font-lock matcher: make the next kept piece before LIMIT read-only.
Every character refuses insertion before it except the last, so
typing right after a piece works but typing inside it doesn't."
  (when (re-search-forward redline--unit-re limit t)
    (let ((beg (or (match-beginning 1) (match-beginning 0)))
          (end (or (match-end 1) (match-end 0))))
      (add-text-properties beg end (list 'read-only redline--unit-message
                                         'redline-unit (list beg)))  ; fresh per piece
      (put-text-property (1- end) end 'rear-nonsticky '(read-only redline-unit)))
    t))

(defconst redline--unit-keywords
  '((redline--fontify-units (0 nil)))
  "Font-lock rule that makes kept pieces read-only.
Font-lock, because cm-mode already lets font-lock manage the read-only
property; anything set another way is wiped at the next redisplay.")

(defun redline--protect-units (on)
  "Make kept pieces read-only (ON non-nil), or editable again."
  (if on
      (progn
        (font-lock-add-keywords nil redline--unit-keywords 'append)
        (dolist (prop '(read-only redline-unit rear-nonsticky))
          (add-to-list 'font-lock-extra-managed-props prop)))
    (font-lock-remove-keywords nil redline--unit-keywords))
  (font-lock-flush))

(defun redline--unit-bounds (pos)
  "Start and end of the kept piece covering POS, or nil."
  (when-let* ((u (and (< pos (point-max)) (get-text-property pos 'redline-unit))))
    (cons (if (and (> pos (point-min)) (eq (get-text-property (1- pos) 'redline-unit) u))
              (previous-single-property-change pos 'redline-unit nil (point-min))
            pos)
          (next-single-property-change pos 'redline-unit nil (point-max)))))

(defun redline--widen-to-units (beg end)
  "BEG and END, pushed out to whole pieces at either edge, as a cons."
  (let ((b (redline--unit-bounds beg))
        (e (and (> end beg) (redline--unit-bounds (1- end)))))
    (cons (if b (min beg (car b)) beg)
          (if e (max end (cdr e)) end))))

(defun redline--delete-char-around (orig n &optional killflag)
  "Around `delete-char': delete a kept piece whole.
ORIG, N, and KILLFLAG are the advised function and its arguments."
  (let* ((beg (if (< n 0) (+ (point) n) (point)))
         (end (if (< n 0) (point) (+ (point) n))))
    (if (not (and (bound-and-true-p redline-mode)
                  (<= (point-min) beg) (<= end (point-max))
                  (progn (font-lock-ensure (line-beginning-position 0) (line-end-position 2))
                         (text-property-not-all beg end 'redline-unit nil))))
        (funcall orig n killflag)
      (let* ((r (redline--widen-to-units beg end))
             (inhibit-read-only t))
        ;; Point where a backspace or a forward delete would leave it,
        ;; so cm-mode records the deletion the same way.
        (goto-char (if (< n 0) (cdr r) (car r)))
        (if killflag (kill-region (car r) (cdr r)) (delete-region (car r) (cdr r)))))))

(defun redline--kill-region-around (orig beg end &rest args)
  "Around `kill-region': a kill that touches a kept piece takes all of it.
ORIG, BEG, END, and ARGS are the advised function and its arguments."
  (if (not (and (bound-and-true-p redline-mode) beg end
                (progn (font-lock-ensure (min beg end) (max beg end))
                       (text-property-not-all (min beg end) (max beg end) 'redline-unit nil))))
      (apply orig beg end args)
    (let ((r (redline--widen-to-units (min beg end) (max beg end)))
          (inhibit-read-only t))
      (apply orig (car r) (cdr r) args))))

;;;; Typing at the end of the file

;; cm-mode's `cm-markup-at-point' steps one character forward to decide
;; between two kinds of markup, which fails at the end of the buffer.
;; With tracking on, that error comes from a before-change hook, and
;; Emacs then drops the hook for good: every later edit goes in
;; untracked, with no message.  At the edges there's no markup to find.

(defun redline--markup-at-point-safe (orig &rest args)
  "Around `cm-markup-at-point': nil instead of an error at a buffer edge.
ORIG and ARGS are the advised function and its arguments."
  (condition-case nil
      (apply orig args)
    ((beginning-of-buffer end-of-buffer) nil)))

;;;; Asking when a .docx is opened

(defun redline--find-file-ask (orig filename &rest args)
  "Around `find-file': offer to review FILENAME in redline if it's a .docx.
ORIG and ARGS are the advised function and its other arguments.
Saying no opens the file the usual way (the document viewer)."
  (if (and (not noninteractive)
           (stringp filename)
           (let ((case-fold-search t)) (string-match-p "\\.docx\\'" filename))
           (file-regular-p filename)
           (y-or-n-p (format "Open %s in redline, as org with its changes and comments? "
                             (file-name-nondirectory filename))))
      (redline-import filename)
    (apply orig filename args)))

;;;###autoload
(define-minor-mode redline-ask-on-docx-mode
  "Ask whether to review a .docx in redline when you open one.
Covers \\[find-file] and its other-window and other-frame versions,
and so dired's RET.  Saying no opens the file the usual way."
  :global t
  :group 'redline
  (dolist (f '(find-file find-file-other-window find-file-other-frame))
    (if redline-ask-on-docx-mode
        (advice-add f :around #'redline--find-file-ask)
      (advice-remove f #'redline--find-file-ask))))

;;;; The minor mode

(declare-function org-return "org" (&optional indent arg interactive))

(defun redline-newline ()
  "Start a new line, tracked when follow-changes is on.
With tracking on, a plain newline goes in.  `newline' (and org's RET,
which calls it) would otherwise move point back a line once cm-mode
has wrapped the newline in markup, so a second RET landed at the
start of the paragraph.  With tracking off, this is org's RET."
  (interactive)
  (if (bound-and-true-p cm-follow-changes-mode)
      (insert "\n")
    (call-interactively (if (derived-mode-p 'org-mode) #'org-return #'newline))))

(defun redline-toggle-tracking ()
  "Switch change tracking on or off, like Word's Track Changes button.
Turns on `redline-mode' first if needed, so the red header shows."
  (interactive)
  (redline--ensure)
  (cm-follow-changes-mode (if cm-follow-changes-mode -1 1))
  (message "Tracking %s" (if cm-follow-changes-mode
                             (format "on, as %s" (or cm-author "no one; set it with C-c * t"))
                           "off")))

(defvar-keymap redline-command-map
  :doc "Redline's review commands, with cm-mode's own beneath them.
`redline-mode' puts this on C-c *.  To have it in every buffer,
including org buffers before redline is on (where org's own C-c *
would turn lines into headings), bind it yourself:

  (keymap-global-set \"C-c *\" redline-command-map)
  (with-eval-after-load \='org
    (keymap-set org-mode-map \"C-c *\" redline-command-map))"
  :parent cm-prefix-map
  "c" #'redline-comment
  "n" #'redline-next-change
  "p" #'redline-previous-change
  "N" #'redline-next-comment
  "P" #'redline-previous-comment
  "RET" #'redline-accept-and-next
  "DEL" #'redline-reject-and-next
  "l" #'redline-pane
  "h" #'redline-toggle-clean-view
  "r" #'redline-reply
  "k" #'redline-delete-comment
  "o" #'redline-import
  "w" #'redline-export
  "F" #'redline-toggle-tracking)

(defvar-keymap redline-mode-map
  :doc "Keys for `redline-mode': its commands and cm-mode's under C-c *."
  "C-c *" redline-command-map
  "<right-margin> <mouse-1>" #'redline-margin-click
  "<remap> <org-return>" #'redline-newline
  "<remap> <org-return-and-maybe-indent>" #'redline-newline
  "<remap> <newline>" #'redline-newline)

;;;; Org targets

;; Two comments in one paragraph, {>>a<<} ... {>>b<<}, contain
;; "<<} ... {>>", which org reads as a <<target>>.  org-modern then
;; draws the stretch between as a grey box with a ↪.  Redline buffers
;; use a target pattern that won't start at } or end at {.

(defvar org-modern-internal-target)
(defvar org-modern-mode)
(declare-function org-modern-mode "ext:org-modern" (&optional arg))

(defconst redline--org-target-regexp
  (let ((edge "[^<>{}\n\r \t]"))
    (format "<<\\(%s\\|%s[^<>\n\r]*%s\\)>>" edge edge edge))
  "Org's target pattern, minus the ones CriticMarkup comments make.")

(defun redline--quiet-org-targets (on)
  "Keep org (and org-modern) from reading comments as targets.
ON non-nil starts that in this buffer; nil puts things back."
  (if on
      (setq-local org-target-regexp redline--org-target-regexp
                  org-modern-internal-target nil)
    (kill-local-variable 'org-target-regexp)
    (kill-local-variable 'org-modern-internal-target))
  ;; org-modern reads its settings when it starts in a buffer.
  (when (bound-and-true-p org-modern-mode)
    (org-modern-mode -1)
    (org-modern-mode 1)))

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
        (redline--quiet-org-targets t)
        (redline--protect-units t)
        (advice-add 'delete-char :around #'redline--delete-char-around)
        (advice-add 'kill-region :around #'redline--kill-region-around)
        (advice-add 'cm-markup-at-point :around #'redline--markup-at-point-safe)
        (redline-pane-setup)
        (when (and redline-track-changes-on-open
                   (redline-docx-file-p)
                   (not cm-follow-changes-mode))
          (cm-follow-changes-mode 1)))
    (redline--quiet-org-targets nil)
    (redline--protect-units nil)
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
