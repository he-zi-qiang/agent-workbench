# Configuration: policy

`policy.default_effect` decides what happens to a tool nothing explicitly
allows, and shipping it as anything but deny would make every future gap a
grant. `policy.write_tools_require_approval` is what puts a human in front of
an outward-facing effect.

`policy.revision` is frozen into each Task at submission.
