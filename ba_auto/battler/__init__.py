"""Smart battler: skill catalog, battle perception, decision policy, shadow mode.

catalog     what each student's EX does, with where that knowledge came from
cards       which student is on each EX card, every frame
field       enemies, allies, clusters and waves from the battlefield picture
policy      pure decisions: what to cast, on what, and why (no game or vision imports)
shadow      watch a battle and log what it would have done, sending no input

The task that sends input is ba_auto/tasks/smartbattle.py.
"""
