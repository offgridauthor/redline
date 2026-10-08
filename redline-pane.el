;;; redline-pane.el --- Review pane, margin tags, and clean view for redline  -*- lexical-binding: t; -*-

;; Copyright (C) 2026 Stephen Lloyd Webber

;; Author: Stephen Lloyd Webber <offgridauthor@gmail.com>
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

;; The Word-style review layer over cm-mode's CriticMarkup, used by
;; `redline-mode':
;;
;; - a review pane listing every change and comment thread, with
;;   accept, reject, resolve, and delete from the pane;
;; - author tags in the right margin beside each commented paragraph;
;; - a clean view that hides the braces and folds comments into markers;
;; - replies, and deleting a single comment from a thread;
;; - a header line while change tracking is on.
;;
;; Nothing here acts at load time; `redline-mode' turns it on per buffer.

;;; Code:

(require 'cm-mode)
(require 'seq)
(require 'subr-x)

;;;; Scanning

(defconst redline--types
  `((cm-addition . ,cm-addition-regexp)
    (cm-deletion . ,cm-deletion-regexp)
    (cm-substitution . ,cm-substitution-regexp)
    (cm-comment . ,cm-comment-regexp)
    (cm-highlight . ,cm-highlight-regexp))
  "CriticMarkup types and the regexps cm-mode uses for them.")

(defconst redline--open-re "{\\(?:\\+\\+\\|--\\|~~\\|>>\\|==\\)"
  "Regexp matching any CriticMarkup opening delimiter.")

(defun redline--scan ()
  "Return every CriticMarkup item in the buffer as a list of plists."
  (let (items)
    (save-excursion
      (save-restriction
        (widen)
        (goto-char (point-min))
        (while (re-search-forward redline--open-re nil t)
          (let* ((start (match-beginning 0))
                 (hit (progn (goto-char start)
                             (seq-find (lambda (ty) (looking-at (cdr ty)))
                                       redline--types))))
            (if (not hit)
                (goto-char (1+ start))
              (let ((type (car hit))
                    (end (match-end 0))
                    (body (match-string-no-properties 1))
                    (new (and (eq (car hit) 'cm-substitution)
                              (match-string-no-properties 2))))
                (push (list :type type :beg start :end end :body body :new new)
                      items)
                (goto-char end)))))))
    (nreverse items)))

(defun redline--parse-comment (body)
  "Split comment BODY into (AUTHOR . TEXT)."
  (if (string-match "\\`@\\([^[:space:]]+\\)[[:space:]]*\\(\\(?:.\\|\n\\)*\\)\\'" body)
      (cons (match-string 1 body) (string-trim (match-string 2 body)))
    (cons nil (string-trim body))))

(defun redline--entries ()
  "Group scanned items into entries.
A comment that directly follows a change (or another comment)
joins that entry's thread, which is how cm-mode attaches authors."
  (let (entries)
    (dolist (it (redline--scan))
      (let* ((prev (car entries))
             (comment-p (eq (plist-get it :type) 'cm-comment))
             ;; A comment is the list (AUTHOR TEXT BEG END).
             (c (when comment-p
                  (let ((ac (redline--parse-comment (plist-get it :body))))
                    (list (car ac) (cdr ac) (plist-get it :beg) (plist-get it :end))))))
        (if (and comment-p prev (= (plist-get it :beg) (plist-get prev :end)))
            (progn
              (plist-put prev :end (plist-get it :end))
              (plist-put prev :comments (append (plist-get prev :comments) (list c))))
          (push (list :type (plist-get it :type)
                      :beg (plist-get it :beg)
                      :end (plist-get it :end)
                      :item-end (plist-get it :end)
                      :body (plist-get it :body)
                      :new (plist-get it :new)
                      :comments (and c (list c)))
                entries))))
    (nreverse entries)))

(defun redline--entry-author (entry)
  "First author tag attached to ENTRY, or nil."
  (car (car (plist-get entry :comments))))

;;;; Faces (light and dark themes)

(defun redline-apply-faces ()
  "Give cm-mode's faces colors that read on light and dark backgrounds."
  (dolist (spec
           '((cm-addition-face
              ((((background dark)) :inherit nil :foreground "#8fd68f" :background "#1d331d" :underline t)
               (t :inherit nil :foreground "#006800" :background "#e3f7e3" :underline t)))
             (cm-deletion-face
              ((((background dark)) :inherit nil :foreground "#ff8f85" :strike-through t)
               (t :inherit nil :foreground "#a60000" :strike-through t)))
             (cm-substitution-face
              ((((background dark)) :inherit nil :foreground "#f2c27a" :background "#33290f")
               (t :inherit nil :foreground "#7a4a00" :background "#fff4dc")))
             (cm-comment-face
              ((((background dark)) :inherit nil :foreground "#a9b9ff" :background "#1b2033" :slant italic :height 0.9)
               (t :inherit nil :foreground "#3548cf" :background "#eef0fb" :slant italic :height 0.9)))
             (cm-highlight-face
              ((((background dark)) :inherit nil :background "#4a4312")
               (t :inherit nil :background "#fff3a3")))))
    (face-spec-set (car spec) (cadr spec))))

;;;; Author colors

(defconst redline--palette-light
  '("#3548cf" "#8f0075" "#00663f" "#a0132f" "#6052cf" "#005e8b"))
(defconst redline--palette-dark
  '("#8fb0ff" "#ff9be0" "#7ee2b8" "#ff9a9a" "#c3b5ff" "#7fd4ff"))

(defun redline--palette ()
  "Colors handed out to authors in order of first appearance."
  (if (eq (frame-parameter nil 'background-mode) 'dark)
      redline--palette-dark redline--palette-light))

(defvar redline--author-colors (make-hash-table :test #'equal))

(defun redline--author-face (author)
  "A face plist that gives AUTHOR a stable color."
  (let* ((key (or author "?"))
         (n (or (gethash key redline--author-colors)
                (puthash key (hash-table-count redline--author-colors) redline--author-colors)))
         (pal (redline--palette))
         (color (nth (mod n (length pal)) pal)))
    `(:foreground ,color :weight bold)))

;;;; Margin tags and clean view

(defvar-local redline--overlays nil)

(defvar-local redline-clean-view nil
  "Non-nil when braces are hidden and comment bodies are folded.")

(defun redline--clear-overlays ()
  "Remove every overlay redline put in this buffer."
  (mapc #'delete-overlay redline--overlays)
  (setq redline--overlays nil))

(defun redline--ov (beg end &rest props)
  "Make an overlay from BEG to END with PROPS, and remember it."
  (let ((ov (make-overlay beg end nil t nil)))
    (overlay-put ov 'review t)
    (overlay-put ov 'evaporate t)
    (while props (overlay-put ov (pop props) (pop props)))
    (push ov redline--overlays)
    ov))

(defun redline--hide-delims (beg end)
  "Hide the CriticMarkup delimiters of the item between BEG and END."
  (save-excursion
    (goto-char beg)
    (redline--ov beg (+ beg 3) 'invisible 'redline-markup)
    (redline--ov (- end 3) end 'invisible 'redline-markup)
    (goto-char (+ beg 3))
    (when (search-forward "~>" (- end 3) t)
      (redline--ov (match-beginning 0) (match-end 0) 'invisible 'redline-markup))))

(defun redline-refresh-overlays ()
  "Redraw margin tags and, in clean view, the hidden braces and folded comments."
  (interactive)
  (redline--clear-overlays)
  (let ((entries (redline--entries))
        (tags (make-hash-table)))
    ;; Margin tags, one per paragraph, naming whoever commented in it.
    ;; A bare {>>@SLW<<} only signs a change, so it doesn't count.
    ;; Tags go on the paragraph's first character: a tag placed
    ;; mid-paragraph can be drawn twice when visual-line-mode wraps.
    (dolist (e entries)
      (dolist (c (plist-get e :comments))
        (unless (string-empty-p (nth 1 c))
          (let ((bol (save-excursion (goto-char (nth 2 c)) (line-beginning-position))))
            (puthash bol (append (gethash bol tags) (list (car c))) tags)))))
    (maphash
     (lambda (bol authors)
       (let* ((n (length authors))
              (label (concat (mapconcat (lambda (a)
                                          (propertize (or a "?") 'face (redline--author-face a)))
                                        (delete-dups (copy-sequence authors)) "·")
                             (if (> n 1) (propertize (format " %d" n) 'face 'shadow) ""))))
         ;; One char wide, because an empty overlay would evaporate at once.
         (redline--ov bol (min (1+ bol) (point-max))
                     'before-string
                     (propertize " " 'display `((margin right-margin) ,label)))))
     tags)
  (dolist (e entries)
    (let ((type (plist-get e :type)))
      (when redline-clean-view
        ;; Hide braces on the change itself (not on bare comments).
        (unless (eq type 'cm-comment)
          (redline--hide-delims (plist-get e :beg) (plist-get e :item-end))
          (when (eq type 'cm-substitution)
            (save-excursion
              (goto-char (plist-get e :beg))
              (when (looking-at cm-substitution-regexp)
                (redline--ov (match-beginning 1) (match-end 1)
                            'face 'cm-deletion-face)
                (redline--ov (match-beginning 2) (match-end 2)
                            'face 'cm-addition-face)))))
        ;; Fold each comment body into a marker; hover or the pane shows it.
        ;; Bare author tags ({>>@SLW<<}) just disappear.
        (dolist (c (plist-get e :comments))
          (if (string-empty-p (nth 1 c))
              (redline--ov (nth 2 c) (nth 3 c) 'invisible 'redline-markup)
            (redline--ov (nth 2 c) (nth 3 c)
                        'display (propertize "◆" 'face (redline--author-face (car c)))
                        'help-echo (format "%s: %s" (or (car c) "?") (nth 1 c))))))))))

(defun redline-toggle-clean-view ()
  "Toggle between raw CriticMarkup and a Word-like clean view."
  (interactive)
  (setq redline-clean-view (not redline-clean-view))
  (add-to-invisibility-spec 'redline-markup)
  (redline-refresh-overlays)
  (message "Clean view %s" (if redline-clean-view "on: braces hidden, comments folded (hover ◆ or C-c * l)" "off: raw markup")))

;;;; Review pane

(defvar-local redline--source nil "Source buffer this pane describes.")

(defconst redline--labels
  '((cm-addition . "Insert") (cm-deletion . "Delete") (cm-substitution . "Replace")
    (cm-comment . "Comment") (cm-highlight . "Comment on")))

(defun redline--wrap (text width indent)
  "Fill TEXT to WIDTH, indenting continuation lines by INDENT spaces."
  (with-temp-buffer
    (insert text)
    (let ((fill-column width)
          (fill-prefix (make-string indent ?\s)))
      (fill-region (point-min) (point-max)))
    (buffer-string)))

(defun redline--snippet (s n)
  "S on one line, cut to N characters."
  (let ((s (replace-regexp-in-string "[\n\t ]+" " " (or s ""))))
    (if (> (length s) n) (concat (substring s 0 (1- n)) "…") s)))

(define-derived-mode redline-pane-mode special-mode "Review"
  "Side pane listing every tracked change and comment."
  (setq truncate-lines nil)
  (setq-local truncate-partial-width-windows nil)   ; a side window is always "partial"
  (setq-local cursor-type 'bar)
  ;; Re-wrap when another side window (org-remark's notes) squeezes this one.
  (add-hook 'window-size-change-functions
            (lambda (_) (when (buffer-live-p (current-buffer)) (redline-pane-refresh)))
            nil t))

(define-key redline-pane-mode-map (kbd "g") #'redline-pane-refresh)
(define-key redline-pane-mode-map (kbd "n") #'forward-button)
(define-key redline-pane-mode-map (kbd "p") #'backward-button)
(define-key redline-pane-mode-map (kbd "TAB") #'forward-button)
(define-key redline-pane-mode-map (kbd "a") #'redline-pane-accept)
(define-key redline-pane-mode-map (kbd "r") #'redline-pane-reject)
(define-key redline-pane-mode-map (kbd "c") #'redline-pane-toggle-resolved)
(define-key redline-pane-mode-map (kbd "k") #'redline-pane-delete-thread)
(define-key redline-pane-mode-map (kbd "A") #'redline-pane-accept-all)
(define-key redline-pane-mode-map (kbd "R") #'redline-pane-reject-all)

(defun redline--pane-buffer (src)
  "The review pane buffer for SRC."
  (get-buffer-create (format "*Review: %s*" (buffer-name src))))

(defun redline-pane-refresh ()
  "Redraw the review pane from its source buffer."
  (interactive)
  (let* ((pane (if (derived-mode-p 'redline-pane-mode) (current-buffer)
                 (redline--pane-buffer (current-buffer))))
         (src (or (buffer-local-value 'redline--source pane) (current-buffer)))
         (entries (with-current-buffer src (redline--entries)))
         (width (max 30 (- (or (and (get-buffer-window pane)
                                    (window-body-width (get-buffer-window pane)))
                               44)
                           2))))
    (with-current-buffer pane
      (let ((inhibit-read-only t)
            (pos (point)))
        (erase-buffer)
        (insert (propertize (format "Review · %s\n" (buffer-name src)) 'face 'bold)
                (propertize (format "%d changes · %d comments\n"
                                    (seq-count (lambda (e) (memq (plist-get e :type)
                                                                 '(cm-addition cm-deletion cm-substitution)))
                                               entries)
                                    (seq-count (lambda (c) (not (string-empty-p (nth 1 c))))
                                               (seq-mapcat (lambda (e) (plist-get e :comments)) entries)))
                            'face 'shadow)
                (propertize "RET jump · g refresh\na accept · r reject\nc resolve · k delete\nA/R accept/reject all\n\n" 'face 'shadow))
        (dolist (e entries)
          (let* ((type (plist-get e :type))
                 (beg (plist-get e :beg))
                 (line (with-current-buffer src (line-number-at-pos beg)))
                 (what (pcase type
                         ('cm-substitution (format "%s → %s"
                                                   (redline--snippet (plist-get e :body) 18)
                                                   (redline--snippet (plist-get e :new) 18)))
                         ('cm-comment nil)
                         (_ (format "“%s”" (redline--snippet (plist-get e :body) 36)))))
                 (head (format "%-10s L%d" (alist-get type redline--labels) line))
                 (block-start (point)))
            (insert-text-button head
                                'face '(:inherit link :underline nil :weight bold)
                                'follow-link t
                                'action (let ((m (with-current-buffer src (copy-marker beg))))
                                          (lambda (_)
                                            (let ((w (get-buffer-window (marker-buffer m))))
                                              (if w (select-window w)
                                                (pop-to-buffer (marker-buffer m))))
                                            (goto-char m)
                                            (pulse-momentary-highlight-one-line (point)))))
            (insert "\n")
            (when what
              (insert (redline--wrap (concat "  " what) width 2) "\n"))
            (dolist (c (plist-get e :comments))
              (let* ((name (or (car c) "?"))
                     (who (propertize name 'face (redline--author-face (car c))))
                     (txt (nth 1 c))
                     (indent (+ 4 (length name))))
                (insert "  " who
                        (if (string-empty-p txt)
                            (propertize "  (made this change)" 'face 'shadow)
                          (concat "  " (propertize
                                        (replace-regexp-in-string
                                         "\n" (concat "\n" (make-string indent ?\s))
                                         (redline--wrap txt (- width indent) 0))
                                        'face (if (string-prefix-p "✓" txt) 'shadow 'default))))
                        "\n")))
            ;; Tag the whole block so the pane can find it from the text.
            (put-text-property block-start (point) 'redline-beg beg)
            (insert "\n")))
        (goto-char (min pos (point-max)))))))

(defun redline-pane ()
  "Show the review pane for the current buffer in a right side window."
  (interactive)
  (let* ((src (current-buffer))
         (pane (redline--pane-buffer src)))
    (with-current-buffer pane
      (unless (derived-mode-p 'redline-pane-mode) (redline-pane-mode))
      (setq redline--source src))
    (display-buffer pane '((display-buffer-in-side-window)
                           (side . right) (slot . 0) (window-width . 46)))
    (with-current-buffer pane (redline-pane-refresh))))

;;;; Linking text and pane

(defvar-local redline--pane-ov nil)

(defun redline--pane-show (beg)
  "Highlight and scroll to the pane entry for the change starting at BEG."
  (when-let* ((pane (get-buffer (format "*Review: %s*" (buffer-name))))
              (win (get-buffer-window pane)))
    (with-current-buffer pane
      (let ((start (text-property-any (point-min) (point-max) 'redline-beg beg)))
        (if (not start)
            (when redline--pane-ov (delete-overlay redline--pane-ov))
          (let ((end (or (next-single-property-change start 'redline-beg) (point-max))))
            (unless (overlayp redline--pane-ov)
              (setq redline--pane-ov (make-overlay 1 1))
              (overlay-put redline--pane-ov 'face 'highlight))
            (move-overlay redline--pane-ov start end pane)
            (set-window-point win start)
            (unless (pos-visible-in-window-p end win)
              (set-window-start win start))))))))

(defun redline--follow-point ()
  "Light up the pane entry for the change or comment under point."
  (when-let* ((e (redline--entry-at (point))))
    (redline--pane-show (plist-get e :beg))))

(defvar-local redline--follow-timer nil)

(defun redline--schedule-follow ()
  "After a pause, light up the pane entry under point."
  (when redline--follow-timer (cancel-timer redline--follow-timer))
  (let ((buf (current-buffer)))
    (setq redline--follow-timer
          (run-with-idle-timer 0.25 nil
                               (lambda ()
                                 (when (and (buffer-live-p buf) (eq buf (current-buffer)))
                                   (redline--follow-point)))))))

(defun redline-margin-click (event)
  "Open the comments for the paragraph whose margin tag EVENT clicked."
  (interactive "e")
  (let* ((posn (event-start event))
         (pos (posn-point posn)))
    (select-window (posn-window posn))
    (when pos
      (goto-char pos)
      (let* ((bol (line-beginning-position))
             (eol (line-end-position))
             (e (seq-find (lambda (e)
                            (and (<= bol (plist-get e :beg) eol)
                                 (seq-some (lambda (c) (not (string-empty-p (nth 1 c))))
                                           (plist-get e :comments))))
                          (redline--entries))))
        (when e
          (goto-char (plist-get e :beg))
          (redline-pane)
          (redline--pane-show (plist-get e :beg)))))))

;;;; Live refresh

(defvar-local redline--timer nil)

(defun redline--after-change (&rest _)
  "After a pause, redraw tags and the pane."
  (when redline--timer (cancel-timer redline--timer))
  (let ((buf (current-buffer)))
    (setq redline--timer
          (run-with-idle-timer
           0.6 nil
           (lambda ()
             (when (buffer-live-p buf)
               (with-current-buffer buf
                 (redline-refresh-overlays)
                 (let ((pane (get-buffer (format "*Review: %s*" (buffer-name buf)))))
                   (when (and pane (get-buffer-window pane))
                     (with-current-buffer pane (redline-pane-refresh)))))))))))

;;;; Comment commands

(defun redline--entry-at (pos)
  "The change or comment entry that covers POS, if any."
  (seq-find (lambda (e) (and (<= (plist-get e :beg) pos) (<= pos (plist-get e :end))))
            (redline--entries)))

(defun redline-reply ()
  "Add a reply to the comment thread at point, as `cm-author'."
  (interactive)
  (let ((e (redline--entry-at (point))))
    (unless (and e (plist-get e :comments))
      (user-error "Point isn't on a comment or a change with comments"))
    (goto-char (plist-get e :end))
    (let ((inhibit-read-only t)
          (start (point)))
      (cm-without-following-changes
        (insert "{>>" (if cm-author (concat "@" cm-author " ") "") "<<}"))
      (font-lock-flush start (point))
      (backward-char 3))
    (redline--after-change)))

(defun redline-delete-comment ()
  "Delete just the one comment at point (not the change it annotates)."
  (interactive)
  (let* ((e (redline--entry-at (point)))
         (c (and e (seq-find (lambda (c) (and (<= (nth 2 c) (point)) (<= (point) (nth 3 c))))
                             (plist-get e :comments)))))
    (unless c (user-error "Point isn't inside a comment"))
    (when (y-or-n-p (format "Delete %s's comment? " (or (car c) "this")))
      (let ((inhibit-read-only t))
        (cm-without-following-changes
          (delete-region (nth 2 c) (nth 3 c))))
      (redline--after-change))))

;;;; Accept, reject, resolve

(defun redline--said (e)
  "E's comments that say something (bare author tags left out)."
  (seq-remove (lambda (c) (string-empty-p (nth 1 c))) (plist-get e :comments)))

(defun redline--chain (comments)
  "COMMENTS written back out as CriticMarkup."
  (mapconcat (lambda (c) (format "{>>%s%s<<}"
                                 (if (car c) (concat "@" (car c)) "")
                                 (if (and (car c) (not (string-empty-p (nth 1 c))))
                                     (concat " " (nth 1 c)) (nth 1 c))))
             comments ""))

(defun redline--resolve (e action)
  "Replace entry E in the current buffer according to ACTION.
ACTION is `accept', `reject', or `delete' (the comments).  Comments on
a change outlive it, the way they do in Word: they move onto the text
that remains, or stay at the spot if no text remains."
  (let* ((type (plist-get e :type))
         (body (plist-get e :body))
         (said (redline--said e))
         (chain (redline--chain said))
         (keep (pcase (list type action)
                 (`(cm-addition accept) body)
                 (`(cm-addition reject) nil)
                 (`(cm-deletion accept) nil)
                 (`(cm-deletion reject) body)
                 (`(cm-substitution accept) (plist-get e :new))
                 (`(cm-substitution reject) body)
                 (`(cm-highlight ,_) body)
                 (`(cm-comment ,_) nil)))
         (text (cond
                ((eq action 'delete) (or keep ""))
                ((and keep said) (concat "{==" keep "==}" chain))
                (keep keep)
                (t chain))))
    (let ((inhibit-read-only t))
      (cm-without-following-changes
        (save-excursion
          (goto-char (plist-get e :beg))
          (delete-region (plist-get e :beg) (plist-get e :end))
          (insert text))))
    (font-lock-flush)
    (redline--after-change)))

(defun redline--pane-entry ()
  "The source buffer and entry for the pane line at point."
  (let* ((beg (get-text-property (point) 'redline-beg))
         (src redline--source))
    (unless beg (user-error "No change or comment on this line"))
    (with-current-buffer src
      (cons src (seq-find (lambda (e) (= (plist-get e :beg) beg)) (redline--entries))))))

(defun redline--pane-act (action)
  "Do ACTION (accept, reject, or delete) to the pane entry at point."
  (pcase-let ((`(,src . ,e) (redline--pane-entry)))
    (unless e (user-error "That entry has moved; press g to refresh"))
    (when (and (memq action '(accept reject))
               (memq (plist-get e :type) '(cm-comment cm-highlight)))
      (user-error "That's a comment: c resolves it, k deletes it"))
    (with-current-buffer src (redline--resolve e action))
    (redline-pane-refresh)))

(defun redline-pane-accept () "Accept the change on this line." (interactive) (redline--pane-act 'accept))
(defun redline-pane-reject () "Reject the change on this line." (interactive) (redline--pane-act 'reject))

(defun redline-pane-delete-thread ()
  "Delete the comments on this line's entry (the text stays)."
  (interactive)
  (when (y-or-n-p "Delete this comment thread? ")
    (redline--pane-act 'delete)))

(defun redline-pane-toggle-resolved ()
  "Mark the thread on this line resolved (✓), or reopen it.
Resolved threads go to Word as resolved comments."
  (interactive)
  (pcase-let ((`(,src . ,e) (redline--pane-entry)))
    (let ((c (car (redline--said e))))
      (unless c (user-error "No comment here"))
      (with-current-buffer src
        (let ((inhibit-read-only t)
              (pos (+ (nth 2 c) 3 (if (car c) (1+ (length (car c))) 0))))
          (cm-without-following-changes
            (save-excursion
              (goto-char pos)
              (skip-chars-forward " ")
              (if (looking-at "✓ ")
                  (delete-region (point) (+ (point) 2))
                (insert "✓ ")))))
        (redline--after-change)))
    (redline-pane-refresh)))

(defun redline--all (action)
  "Do ACTION (accept or reject) to every change in the source buffer."
  (let ((src redline--source))
    (with-current-buffer src
      (dolist (e (reverse (redline--entries)))
        (when (memq (plist-get e :type) '(cm-addition cm-deletion cm-substitution))
          (redline--resolve e action))))
    (redline-pane-refresh)))

(defun redline-pane-accept-all ()
  "Accept every tracked change in the source buffer."
  (interactive)
  (when (y-or-n-p "Accept every change? ") (redline--all 'accept)))

(defun redline-pane-reject-all ()
  "Reject every tracked change in the source buffer."
  (interactive)
  (when (y-or-n-p "Reject every change? ") (redline--all 'reject)))

;;;; Editing comments while tracking

;; With follow-changes on, cm-mode treats typing inside a comment as a
;; new insertion and pushes it outside the comment.  Comments are
;; conversation, not manuscript, so edits inside them go through as-is.

(defun redline--inside-comment-p (pos)
  "Non-nil if POS is inside the braces of a comment.
Searches for the nearest delimiters directly; `cm-markup-at-point'
misreads a comment that touches other markup, as replies do."
  (save-excursion
    (goto-char pos)
    (let* ((lim 20000)
           (open (save-excursion (search-backward "{>>" (max (point-min) (- pos lim)) t)))
           (shut (save-excursion (search-backward "<<}" (max (point-min) (- pos lim)) t))))
      (and open
           (or (null shut) (> open shut))       ; the last opener is still open
           (>= pos (+ open 3))
           (let ((end (save-excursion (search-forward "<<}" (min (point-max) (+ pos lim)) t)))
                 (next (save-excursion (search-forward "{>>" (min (point-max) (+ pos lim)) t))))
             (and end (<= pos (- end 3)) (or (null next) (> next end))))))))

(defun redline--skip-comment-edits (orig beg end)
  "Call ORIG with BEG and END unless the edit is inside a comment."
  (unless (redline--inside-comment-p beg)
    (funcall orig beg end)))

;; The same fix is offered to cm-mode upstream; once cm-mode includes it
;; (it defines `cm-inside-comment-p'), `redline-mode' leaves this off.
(defun redline--install-comment-fix ()
  "Leave edits inside comments untracked, unless cm-mode already does."
  (unless (or (fboundp 'cm-inside-comment-p)
              (advice-member-p #'redline--skip-comment-edits 'cm-before-change))
    (advice-add 'cm-before-change :around #'redline--skip-comment-edits)))

;;;; Follow-changes header line

(defun redline--follow-header ()
  "Show a header line while change tracking is on."
  (setq header-line-format
        (when cm-follow-changes-mode
          (list (propertize (format " ● TRACKING CHANGES as %s " (or cm-author "anonymous"))
                            'face '(:background "#a0132f" :foreground "white" :weight bold))
                (propertize "   C-c * F to stop" 'face 'shadow)))))

;;;; Wiring

(defun redline-pane-setup ()
  "Turn on margin tags, live refresh, and pane following in this buffer."
  (setq-local right-margin-width (max right-margin-width 12))
  (when-let* ((w (get-buffer-window (current-buffer))))
    (set-window-buffer w (current-buffer)))
  (add-to-invisibility-spec 'redline-markup)
  (add-hook 'after-change-functions #'redline--after-change nil t)
  (add-hook 'post-command-hook #'redline--schedule-follow nil t)
  (add-hook 'cm-follow-changes-mode-hook #'redline--follow-header nil t)
  (add-hook 'desktop-save-hook #'redline-close-panes)
  (redline--install-comment-fix)
  (redline--follow-header)
  (redline-refresh-overlays))

(defun redline-pane-teardown ()
  "Undo `redline-pane-setup' in this buffer."
  (redline--clear-overlays)
  (remove-hook 'after-change-functions #'redline--after-change t)
  (remove-hook 'post-command-hook #'redline--schedule-follow t)
  (remove-hook 'cm-follow-changes-mode-hook #'redline--follow-header t)
  (setq header-line-format nil)
  (kill-local-variable 'right-margin-width)
  (when-let* ((w (get-buffer-window (current-buffer))))
    (set-window-buffer w (current-buffer))))

(defun redline-close-panes ()
  "Close review panes before the desktop is saved.
A saved desktop restores side windows badly."
  (dolist (w (window-list-1 nil 'nomini t))
    (when (string-prefix-p "*Review: " (buffer-name (window-buffer w)))
      (ignore-errors (delete-window w)))))

(provide 'redline-pane)
;;; redline-pane.el ends here
