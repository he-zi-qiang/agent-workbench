# Environment: production

Production runs one API process per zone and a single Task Worker, deliberately
-- concurrency is a coordination property and is bought by adding workers only
after the lease behaviour has been measured under the current one.

Identity arrives from the provider; header-supplied principals are refused.
