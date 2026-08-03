"""What the console reads.

One module, one output: ``apps/console/public/replay/replay.json``. The console
is a static Next.js app on Vercel and this is the only shape it consumes, so
everything the EOC screen knows about Melissa is either in that file or is not
on the screen.

Deliberately empty of imports. ``python -m app.console.export`` runs the module
as ``__main__``, and a package that has already imported it under its real name
makes runpy warn about executing it twice.
"""
