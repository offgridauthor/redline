;;; redline-test.el --- Tests for redline  -*- lexical-binding: t; -*-

;; Copyright (C) 2026 Stephen Lloyd Webber
;; SPDX-License-Identifier: GPL-3.0-or-later

;;; Commentary:

;; Run with `make test', or:
;;   emacs -Q --batch -L . -L PATH/TO/cm-mode -l test/redline-test.el \
;;         -f ert-run-tests-batch-and-exit

;;; Code:

(require 'ert)
(require 'org)
(require 'cm-mode)
(require 'redline)

(defmacro redline-test-with (text &rest body)
  "Run BODY in an org buffer holding TEXT, with `redline-mode' on as SLW."
  (declare (indent 1))
  `(with-temp-buffer
     (org-mode)
     (insert ,text)
     (redline-mode 1)
     (setq cm-author "SLW")
     (font-lock-ensure)
     (goto-char (point-min))
     ,@body))

(ert-deftest redline-test-entries-group-threads ()
  (redline-test-with "A {++b++}{>>@SLW<<} c {==d==}{>>@Dana Why?<<}{>>@SLW Because.<<} e{>>@Dana Note<<}"
    (let ((es (redline--entries)))
      (should (= (length es) 3))
      (should (equal (mapcar (lambda (e) (plist-get e :type)) es)
                     '(cm-addition cm-highlight cm-comment)))
      (should (equal (mapcar #'car (plist-get (nth 1 es) :comments)) '("Dana" "SLW"))))))

(ert-deftest redline-test-tracking-records-typing ()
  (redline-test-with "One two three."
    (cm-follow-changes-mode 1)
    (search-forward "two")
    (insert " and")
    (should (string-match-p (regexp-quote "two{++ and++}{>>@SLW<<}") (buffer-string)))))

(ert-deftest redline-test-tracking-leaves-comment-edits-alone ()
  (redline-test-with "Text {==here==}{>>@SLW Love this.<<} more."
    (cm-follow-changes-mode 1)
    (search-forward "Love this")
    (insert " a lot")
    (should (string-match-p (regexp-quote "{>>@SLW Love this a lot.<<}") (buffer-string)))
    (should-not (string-match-p (regexp-quote "{++") (buffer-string)))))

(ert-deftest redline-test-comments-are-not-targets ()
  (redline-test-with "A {++b++}{>>@SLW<<} c {--d--}{>>@SLW<<} e <<real>>."
    (should-not (string-match-p org-target-regexp "{>>@SLW<<} c {--d--}{>>"))
    (should (string-match-p org-target-regexp "<<real>>"))
    (search-forward "c {")
    (should-not (eq (get-text-property (point) 'face) 'org-target))))

(ert-deftest redline-test-comment-on-comment-replies ()
  (redline-test-with "X {==y==}{>>@Copyedit Which?<<} z."
    (search-forward "Whi")
    (redline-comment)
    (insert "This one.")
    (should (string-match-p (regexp-quote "{>>@Copyedit Which?<<}{>>@SLW This one.<<}")
                            (buffer-string)))))

(ert-deftest redline-test-comment-region-trims-markup ()
  (redline-test-with "X {==y==}{>>@Copyedit Which order?<<} then plain words."
    (search-forward "Which")
    (push-mark (point) t t)
    (search-forward "plain")
    (redline-comment (region-beginning) (region-end))
    (insert "note")
    (should (string-match-p (regexp-quote "<<} {==then plain==}{>>@SLW note<<} words.")
                            (buffer-string)))))

(ert-deftest redline-test-comment-across-paragraphs-and-changes ()
  (redline-test-with "One two.\n\n* Head\n\nThree {++x++}{>>@SLW<<} four five."
    (let ((beg (progn (search-forward "One ") (point)))
          (end (progn (search-forward "four") (point))))
      (redline-comment beg end))
    (insert "across")
    (should (equal (buffer-string)
                   (concat "One {==two.==}\n\n* {==Head==}\n\n{==Three==} {++x++}{>>@SLW<<} "
                           "{==four==}{>>@SLW across<<} five.")))
    (let ((es (seq-filter (lambda (e) (eq (plist-get e :type) 'cm-highlight))
                          (redline--entries))))
      (should (= (length es) 1))
      (should (= (length (plist-get (car es) :frags)) 3))
      ;; Deleting the comment takes every piece's braces with it.
      (redline--resolve (car es) 'delete)
      (should (equal (buffer-string)
                     "One two.\n\n* Head\n\nThree {++x++}{>>@SLW<<} four five.")))))

(ert-deftest redline-test-tracked-paragraph-split ()
  (redline-test-with "Zero here.\n\nOne. Two."
    (cm-follow-changes-mode 1)
    (search-forward "One. ")
    (call-interactively (key-binding (kbd "RET")))
    (call-interactively (key-binding (kbd "RET")))
    (should (equal (buffer-string) "Zero here.\n\nOne. {++\n\n++}{>>@SLW<<}Two."))))

(ert-deftest redline-test-next-and-previous ()
  (redline-test-with "A {++b++}{>>@SLW<<}{--tech--}{>>@SLW<<} too {==x==}{>>@Copyedit q<<}{>>@SLW r<<} end {~~a~>b~~}{>>@D<<} {>>@Dana Note<<}."
    (cl-flet ((stops (fn) (goto-char (point-min))
                (let (ps) (condition-case nil (while t (funcall fn) (push (point) ps))
                            (user-error nil))
                     (nreverse ps))))
      ;; Tracked changes: one stop each, author tags and all.
      (should (= (length (stops #'redline-next-change)) 3))
      ;; Comments: the thread on x, and Dana's note; bare tags don't count.
      (should (= (length (stops #'redline-next-comment)) 2)))
    (goto-char (point-max))
    (redline-previous-change 2)
    (should (looking-at-p (regexp-quote "{--tech--}")))
    (goto-char (point-min))
    (redline-next-change)
    (redline-accept-and-next)
    (should (string-prefix-p "A b{--tech--}" (buffer-string)))
    (should (looking-at-p (regexp-quote "{--tech--}")))))

(ert-deftest redline-test-keys-work-before-the-mode ()
  (should (eq (keymap-lookup redline-command-map "c") #'redline-comment))
  (should (eq (keymap-lookup redline-command-map "a") #'cm-addition)) ; cm-mode's, beneath
  (with-temp-buffer
    (org-mode)
    (insert "Plain org, no markup yet.")
    (setq-local cm-author "SLW")
    (should-not (bound-and-true-p redline-mode))
    (goto-char (point-max))
    (redline-comment)
    (insert "first note")
    (should redline-mode)
    (should (string-suffix-p "{>>@SLW first note<<}" (buffer-string)))))

(ert-deftest redline-test-reply-adds-to-thread ()
  (redline-test-with "X {==y==}{>>@Dana Should I?<<} z."
    (search-forward "Should")
    (redline-reply)
    (insert "Yes.")
    (should (string-match-p (regexp-quote "{>>@Dana Should I?<<}{>>@SLW Yes.<<}") (buffer-string)))))

(ert-deftest redline-test-delete-one-comment ()
  (redline-test-with "X {==y==}{>>@Dana Should I?<<}{>>@SLW Yes.<<} z."
    (search-forward "Yes")
    (cl-letf (((symbol-function 'y-or-n-p) (lambda (&rest _) t)))
      (redline-delete-comment))
    (should (equal (buffer-string) "X {==y==}{>>@Dana Should I?<<} z."))))

(ert-deftest redline-test-accept-and-reject ()
  (dolist (case '(("a {++b++}{>>@SLW<<} c" accept "a b c")
                  ("a {++b++}{>>@SLW<<} c" reject "a  c")
                  ("a {--b--}{>>@SLW<<} c" accept "a  c")
                  ("a {--b--}{>>@SLW<<} c" reject "a b c")
                  ("a {~~b~>B~~}{>>@SLW<<} c" accept "a B c")
                  ("a {~~b~>B~~}{>>@SLW<<} c" reject "a b c")
                  ;; Comments outlive the change, as in Word.
                  ("a {++b++}{>>@SLW Why not.<<} c" accept "a {==b==}{>>@SLW Why not.<<} c")
                  ("a {--b--}{>>@SLW Cut.<<} c" accept "a {>>@SLW Cut.<<} c")))
    (redline-test-with (car case)
      (redline--resolve (car (redline--entries)) (nth 1 case))
      (should (equal (buffer-string) (nth 2 case))))))

(ert-deftest redline-test-clean-view-keeps-text ()
  (redline-test-with "a {++b++}{>>@SLW<<} {==c==}{>>@SLW Hm.<<} d"
    (let ((before (buffer-string)))
      (redline-toggle-clean-view)
      (should redline-clean-view)
      (should (equal (buffer-string) before))
      (redline-toggle-clean-view)
      (should (equal (buffer-string) before)))))

(ert-deftest redline-test-round-trip-from-emacs ()
  "Import the sample .docx, make a tracked edit and a reply, export, read back."
  (let* ((dir (make-temp-file "redline" t))
         (src (expand-file-name "client.docx" dir))
         (user-full-name "Stephen Lloyd Webber"))
    (copy-file (expand-file-name "test/fixtures/client.docx"
                                 (file-name-directory redline-script))
               src)
    (add-hook 'org-mode-hook #'redline-maybe-enable)
    (unwind-protect
        (progn
          (redline-import src)
          (should cm-follow-changes-mode)          ; tracking comes on for client files
          (setq cm-author "SLW")
          (goto-char (point-min))
          (search-forward "Then she took the key off the nail.")
          (insert " ")
          (insert "It was cold.")
          (goto-char (point-min))
          (search-forward "Should I say which three")
          (redline-reply)
          (insert "Don't name them yet.")
          (redline-export)
          (let* ((out (car (directory-files dir t "-SLW-.*\\.docx\\'")))
                 (back (expand-file-name "back.org" dir)))
            (should out)
            (should (eql 0 (call-process redline-python nil nil nil redline-script
                                         "import" out "-o" back "--me" "SLW=Stephen Lloyd Webber")))
            (with-temp-buffer
              (insert-file-contents back)
              (should (search-forward "{++ It was cold.++}{>>@SLW<<}" nil t))
              (goto-char (point-min))
              (should (search-forward "{>>@SLW Don't name them yet.<<}" nil t)))))
      (dolist (b (buffer-list))
        (when (and (buffer-file-name b) (string-prefix-p dir (buffer-file-name b)))
          (with-current-buffer b (set-buffer-modified-p nil))
          (kill-buffer b)))
      (remove-hook 'org-mode-hook #'redline-maybe-enable)
      (delete-directory dir t))))

(provide 'redline-test)
;;; redline-test.el ends here
